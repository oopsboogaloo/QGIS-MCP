"""Answers read-only introspection queries inside a standalone QGIS, then exits.

Task 2. Like ``runner.py`` this executes in the **child** process under the
QGIS-bundled interpreter, is launched by path rather than with ``-m``, and
imports nothing but the standard library, ``qgis.*`` and the shared
``qgis_mcp_protocol`` package staged alongside it.

The five operations correspond to the five tools of the narrative spec §4.2.
Keeping them here, as ordinary PyQGIS in a readable file, is the alternative to
the server assembling PyQGIS source as strings -- the same reason ``runner.py``
takes code as data rather than templating it.

**Accessors are probed rather than assumed.** ``design.md`` §7 names version
drift as a standing risk: PyQGIS tracks the C++ API and both move each release.
So an optional accessor that has moved degrades the one field that depended on
it instead of failing the whole call, and the field is simply absent rather
than silently null. A caller can tell "QGIS did not report this" from "QGIS
reported nothing here".

Usage: ``python introspect.py <request.json>``
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

#: Operations this script answers. The server validates against the same list.
OPERATIONS = (
    "project_summary",
    "layer_summary",
    "layers_tree",
    "missing_layers",
    "project_path_info",
)


def _emit(envelope) -> None:
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def _try(getter, default=None):
    """Call ``getter``, returning ``default`` if the accessor is unavailable.

    Guards against PyQGIS API drift only -- it deliberately does not catch
    everything, so a genuine failure inside an accessor still surfaces.
    """
    try:
        return getter()
    except (AttributeError, TypeError):
        return default


def _source_path(source: str) -> str:
    """The filesystem path from a layer source, stripped of provider options.

    A layer URI carries provider options after a pipe --
    ``vectors.gpkg|layername=tracks`` -- and only the part before it is a path.
    """
    return (source or "").split("|", 1)[0]


def _rectangle(extent) -> list[float] | None:
    """A ``QgsRectangle`` as ``[xmin, ymin, xmax, ymax]``, or ``None``."""
    if extent is None or extent.isNull() or extent.isEmpty():
        return None
    return [
        float(extent.xMinimum()),
        float(extent.yMinimum()),
        float(extent.xMaximum()),
        float(extent.yMaximum()),
    ]


def _layer_type_name(layer) -> str:
    """``Vector``, ``Raster``, ``Mesh`` and so on."""
    return _try(lambda: layer.type().name, "Unknown") or "Unknown"


def _field_schema(layer) -> dict:
    """Field name to QGIS type name.

    Includes the GeoPackage ``fid`` primary key, because PyQGIS does. OGR
    treats it as the feature id and omits it, so the two readers legitimately
    disagree -- what matters is reporting what QGIS itself will see.
    """
    return {
        field.name(): field.typeName() for field in _try(lambda: layer.fields(), []) or []
    }


def _describe_layer(layer, *, detailed: bool) -> dict:
    """One layer, at summary or full detail."""
    from qgis.core import Qgis, QgsWkbTypes

    source = _try(lambda: layer.source(), "") or ""
    entry: dict = {
        "name": layer.name(),
        "id": _try(lambda: layer.id()),
        "layer_type": _layer_type_name(layer),
        "is_valid": bool(layer.isValid()),
        "crs": _try(lambda: layer.crs().authid()) or None,
        "provider": _try(lambda: layer.providerType()),
        "source": source,
        "source_path": _source_path(source),
        "extent": _rectangle(_try(lambda: layer.extent())),
    }

    layer_type = _try(lambda: layer.type())
    if layer_type == _try(lambda: Qgis.LayerType.Vector):
        entry["geometry_type"] = _try(
            lambda: QgsWkbTypes.displayString(layer.wkbType())
        )
        # featureCount() is -1 when the provider cannot count without a scan,
        # which is a different fact from "no features".
        count = _try(lambda: layer.featureCount())
        entry["feature_count"] = None if count is None or count < 0 else int(count)
        fields = _field_schema(layer)
        entry["fields"] = fields
        entry["field_count"] = len(fields)
    elif layer_type == _try(lambda: Qgis.LayerType.Raster):
        entry["width"] = _try(lambda: int(layer.width()))
        entry["height"] = _try(lambda: int(layer.height()))
        entry["band_count"] = _try(lambda: int(layer.bandCount()))
        provider = _try(lambda: layer.dataProvider())
        if provider is not None:
            entry["data_type"] = _try(lambda: provider.dataType(1).name)
            if _try(lambda: provider.sourceHasNoDataValue(1), False):
                entry["nodata_value"] = _try(
                    lambda: float(provider.sourceNoDataValue(1))
                )

    if detailed:
        entry["renderer_type"] = _try(lambda: layer.renderer().type())
        # Provenance written by task 4.7's registration step. Surfaced here so
        # REQ-13's vertical datum is readable through the ordinary
        # introspection tool rather than only by whoever wrote it -- which is
        # what lets model_sea_level_change be told the datum at all.
        provenance = _provenance(layer)
        if provenance:
            entry["acquisition_provenance"] = provenance
            if "vertical_datum" in provenance:
                entry["vertical_datum"] = provenance["vertical_datum"]
        if not layer.isValid():
            entry["invalid_reason"] = _invalid_reason(entry["source_path"])
    return entry


#: Prefix task 4.7's registration writes its provenance under.
PROVENANCE_PREFIX = "qgis_mcp/"


def _provenance(layer) -> dict:
    """Acquisition provenance recorded on a layer, if any."""
    keys = _try(lambda: layer.customPropertyKeys(), []) or []
    recorded = {}
    for key in keys:
        if not str(key).startswith(PROVENANCE_PREFIX):
            continue
        value = _try(lambda: layer.customProperty(key))
        if value is not None:
            recorded[str(key)[len(PROVENANCE_PREFIX):]] = value
    return recorded


def _invalid_reason(source_path: str) -> str:
    """Why a layer did not load, as concretely as can be determined.

    "File not found" and "file present but unreadable" send the reader to
    different places -- a moved dataset versus a corrupt or unsupported one --
    so the distinction is made rather than reported as a single "invalid".
    """
    if not source_path:
        return "the layer has no file-based source; the provider may be unavailable"
    if not os.path.exists(source_path):
        return "the referenced file does not exist at this path"
    return (
        "the referenced file exists but could not be opened -- it may be "
        "corrupt, or its format may need a provider that is not available"
    )


def _project_extent(project, layers) -> tuple[list[float] | None, str | None]:
    """Union of the valid layers' extents, in the project CRS.

    Layers are transformed to the project CRS before combining. Combining raw
    extents from mixed CRSs produces a rectangle in no coordinate system at
    all -- a number that looks like an extent and is not one.
    """
    from qgis.core import QgsCoordinateTransform

    target = _try(lambda: project.crs())
    union = None
    for layer in layers:
        if not layer.isValid():
            continue
        extent = _try(lambda: layer.extent())
        if extent is None or extent.isNull() or extent.isEmpty():
            continue
        layer_crs = _try(lambda: layer.crs())
        if (
            target is not None
            and layer_crs is not None
            and target.isValid()
            and layer_crs.isValid()
            and layer_crs != target
        ):
            try:
                transform = QgsCoordinateTransform(
                    layer_crs, target, project.transformContext()
                )
                extent = transform.transformBoundingBox(extent)
            except Exception:
                # An untransformable extent is skipped rather than mixed in.
                continue
        if union is None:
            union = extent
        else:
            union.combineExtentWith(extent)

    authid = _try(lambda: target.authid()) if target is not None else None
    return _rectangle(union), authid


def _describe_tree(node) -> list[dict]:
    """The layer tree under ``node``, groups and nesting included."""
    from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer

    described: list[dict] = []
    for child in node.children():
        if isinstance(child, QgsLayerTreeGroup):
            described.append(
                {
                    "kind": "group",
                    "name": child.name(),
                    "visible": bool(child.itemVisibilityChecked()),
                    "children": _describe_tree(child),
                }
            )
        elif isinstance(child, QgsLayerTreeLayer):
            layer = _try(lambda: child.layer())
            described.append(
                {
                    "kind": "layer",
                    "name": child.name(),
                    "visible": bool(child.itemVisibilityChecked()),
                    "layer_id": _try(lambda: child.layerId()),
                    "layer_type": _layer_type_name(layer) if layer else None,
                    "is_valid": bool(layer.isValid()) if layer else False,
                }
            )
    return described


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def op_project_summary(project, _request) -> dict:
    """Layer counts by type, CRS, extent, active layer, layout count (§4.2)."""
    layers = list(project.mapLayers().values())

    counts: dict = {}
    for layer in layers:
        key = _layer_type_name(layer).lower()
        counts[key] = counts.get(key, 0) + 1

    extent, extent_crs = _project_extent(project, layers)
    layouts = _try(lambda: project.layoutManager().layouts(), []) or []

    return {
        "title": _try(lambda: project.title()) or None,
        "file_path": _try(lambda: project.fileName()) or None,
        "crs": _try(lambda: project.crs().authid()) or None,
        "layer_count": len(layers),
        "layer_counts": counts,
        "invalid_layer_count": sum(1 for layer in layers if not layer.isValid()),
        "extent": extent,
        "extent_crs": extent_crs,
        "layout_count": len(layouts),
        "layouts": [_try(lambda: item.name()) for item in layouts],
        # The active layer is a property of the QGIS GUI, not of a project
        # file: it lives on the interface, and a headless project has no
        # selection. Reported as explicitly unavailable rather than as null,
        # which would read as "no layer is active".
        "active_layer": None,
        "active_layer_available": False,
        "active_layer_note": (
            "the active layer is a property of the QGIS interface, not of the "
            "project file, so it is only available in live mode"
        ),
    }


def op_layer_summary(project, request) -> dict:
    """Full detail for one named layer (§4.2)."""
    name = request.get("layer")
    matches = _try(lambda: project.mapLayersByName(name), []) or []

    if not matches:
        # The requirements' recurring contract (section 0): resolve an
        # underdetermined input by returning the candidates, never by guessing
        # and never failing bare. An unknown layer name is exactly that case.
        available = sorted(layer.name() for layer in project.mapLayers().values())
        raise _Candidates(
            f"no layer named {name!r} in this project.",
            available_layers=available,
        )

    if len(matches) > 1:
        # Layer names are not unique in QGIS. Picking one would be a guess the
        # caller cannot detect, so the ids are returned to disambiguate with.
        raise _Candidates(
            f"{len(matches)} layers are named {name!r}; ask again by id.",
            matching_layer_ids=[_try(lambda: layer.id()) for layer in matches],
        )

    return _describe_layer(matches[0], detailed=True)


def op_layers_tree(project, _request) -> dict:
    """The layer panel structure, including groups and nesting (§4.2)."""
    root = project.layerTreeRoot()
    return {
        "tree": _describe_tree(root),
        "layer_count": len(project.mapLayers()),
    }


def op_missing_layers(project, _request) -> dict:
    """Broken data source references (§4.2)."""
    missing = [
        _describe_layer(layer, detailed=True)
        for layer in project.mapLayers().values()
        if not layer.isValid()
    ]
    missing.sort(key=lambda entry: entry["name"])
    return {
        "missing_count": len(missing),
        "missing_layers": missing,
        "layer_count": len(project.mapLayers()),
    }


def op_project_path_info(project, _request) -> dict:
    """Project file path, save state, CRS, last modified (§4.2)."""
    file_path = _try(lambda: project.fileName()) or None
    info: dict = {
        "file_path": file_path,
        "title": _try(lambda: project.title()) or None,
        "crs": _try(lambda: project.crs().authid()) or None,
        "home_path": _try(lambda: project.homePath()) or None,
        # Always False on this path: the project was read moments ago and
        # nothing has touched it. In live mode it is the question that matters.
        "is_dirty": bool(_try(lambda: project.isDirty(), False)),
        "last_saved_by_qgis_version": _try(
            lambda: project.lastSaveVersion().text()
        )
        or None,
        "last_save_date_time": _try(
            lambda: project.lastSaveDateTime().toString("yyyy-MM-ddTHH:mm:ss")
        )
        or None,
        # Relative storage is what makes a project portable; absolute paths
        # resolve only on the machine that wrote them.
        "file_path_storage": _try(lambda: project.filePathStorage().name),
    }

    if file_path and os.path.exists(file_path):
        stat = os.stat(file_path)
        info["exists"] = True
        info["file_size_bytes"] = stat.st_size
        info["file_modified"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat()
        info["directory"] = os.path.dirname(file_path)
    else:
        info["exists"] = False

    return info


HANDLERS = {
    "project_summary": op_project_summary,
    "layer_summary": op_layer_summary,
    "layers_tree": op_layers_tree,
    "missing_layers": op_missing_layers,
    "project_path_info": op_project_path_info,
}


class _Candidates(Exception):
    """Raised to return candidates for an underdetermined input."""

    def __init__(self, message: str, **detail) -> None:
        super().__init__(message)
        self.detail = detail


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"introspect expects exactly one argument, the request file "
                f"path; got {len(argv) - 1}",
                mode=ExecutionMode.HEADLESS,
            )
        )
        return 2

    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"could not read request file {argv[1]}: {exc}",
                mode=ExecutionMode.HEADLESS,
            )
        )
        return 2

    operation = request.get("operation")
    if operation not in HANDLERS:
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"unknown operation {operation!r}",
                mode=ExecutionMode.HEADLESS,
                supported_operations=list(OPERATIONS),
            )
        )
        return 2

    project_path = request.get("project_path")
    if not project_path:
        # Every §4.2 tool reports on a project, so headless needs one. REQ-2
        # makes the project path the headless path's input, and saying so is
        # more use than reporting an empty project as though it were real.
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"{operation} needs a project. Pass project_path, or configure "
                f"one, so there is something to report on.",
                mode=ExecutionMode.HEADLESS,
            )
        )
        return 2

    from qgis.core import QgsApplication, QgsProject

    prefix_path = request.get("prefix_path") or os.environ.get("QGIS_PREFIX_PATH")
    if prefix_path:
        QgsApplication.setPrefixPath(str(prefix_path), True)
    app = QgsApplication([], False)
    app.initQgis()

    try:
        project = QgsProject.instance()
        # A project with broken layer references must stay introspectable --
        # it is the only kind worth asking get_missing_layers about. So a
        # read() that reports trouble is recorded rather than treated as fatal,
        # and only a project that produced no layers at all is unreadable.
        #
        # Note read() is not a reliable signal of layer health: it returns True
        # for a project whose every layer failed to load. Layer validity is
        # checked directly below instead.
        read_ok = bool(project.read(str(project_path)))
        if not read_ok and not project.mapLayers():
            _emit(
                error(
                    ErrorCode.BAD_REQUEST,
                    f"could not read project file: {project_path}",
                    mode=ExecutionMode.HEADLESS,
                    project_path=str(project_path),
                )
            )
            return 1

        try:
            result = HANDLERS[operation](project, request)
        except _Candidates as candidates:
            _emit(
                error(
                    ErrorCode.BAD_REQUEST,
                    str(candidates),
                    mode=ExecutionMode.HEADLESS,
                    operation=operation,
                    **candidates.detail,
                )
            )
            return 1

        _emit(
            ok(
                result,
                mode=ExecutionMode.HEADLESS,
                operation=operation,
                project_path=str(project_path),
                # Derived from the layers themselves, not from read(). A
                # project whose every layer is broken still reads "True", so
                # taking read()'s word for it would report success over a
                # project where nothing resolved.
                all_layers_loaded=all(
                    layer.isValid() for layer in project.mapLayers().values()
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        _emit(
            error(
                ErrorCode.CHILD_FAILED,
                f"{type(exc).__name__}: {exc}",
                mode=ExecutionMode.HEADLESS,
                operation=operation,
                traceback=traceback.format_exc(),
            )
        )
        return 1
    finally:
        app.exitQgis()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
