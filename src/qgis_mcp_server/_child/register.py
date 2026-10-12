"""Registers an acquired layer into a QGIS project, with its provenance.

Task 4.7's last step. Executes in the **child** process under the QGIS-bundled
interpreter, launched by path, importing only the standard library, ``qgis.*``
and the shared ``qgis_mcp_protocol`` package.

**REQ-13 is why this exists as a step at all.** A reprojected GeoTIFF on disk
carries its horizontal CRS and nothing about its *vertical* datum -- the file
format has nowhere to put it. REQ-13 requires the datum recorded as layer
metadata so that ``model_sea_level_change`` can warn on an unknown or mismatched
one later, and the only place that survives is the project. So the datum, and
the rest of the provenance that would otherwise be lost the moment the download
finished, is written as custom layer properties.

Everything written here persists in the ``.qgz`` and can be read back by
``get_layer_summary``.

Usage: ``python register.py <request.json>``
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

#: Prefix for every custom property this writes, so provenance is identifiable
#: as ours and cannot collide with a user's own properties.
PROPERTY_PREFIX = "qgis_mcp/"

#: The provenance fields recorded on a registered layer.
#:
#: ``vertical_datum`` is the REQ-13 one and the reason the rest are here too:
#: once a step exists to record it, recording what the layer came from costs
#: nothing and answers "where did this file come from" months later, which no
#: amount of looking at the raster will.
PROVENANCE_FIELDS = (
    "vertical_datum",
    "provider",
    "dataset_identifier",
    "dataset_title",
    "licence_tier",
    "licence_summary",
    "attribution",
    "original_path",
    "source_crs",
    "target_crs",
    "crs_tier",
    "aoi",
    "acquired_resolution_m",
    "resampled",
)


def _emit(envelope) -> None:
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"register expects one argument, the request file path; got "
                f"{len(argv) - 1}",
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

    layer_path = request.get("layer_path")
    layer_name = request.get("layer_name")
    project_path = request.get("project_path")
    provenance = request.get("provenance") or {}

    for field, value in (("layer_path", layer_path), ("layer_name", layer_name)):
        if not value:
            _emit(
                error(
                    ErrorCode.BAD_REQUEST,
                    f"{field} is required",
                    mode=ExecutionMode.HEADLESS,
                )
            )
            return 2

    if not os.path.exists(layer_path):
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"the layer file does not exist: {layer_path}",
                mode=ExecutionMode.HEADLESS,
                layer_path=layer_path,
            )
        )
        return 1

    from qgis.core import Qgis, QgsApplication, QgsProject, QgsRasterLayer

    prefix_path = request.get("prefix_path") or os.environ.get("QGIS_PREFIX_PATH")
    if prefix_path:
        QgsApplication.setPrefixPath(str(prefix_path), True)
    app = QgsApplication([], False)
    app.initQgis()

    try:
        project = QgsProject.instance()
        created = False

        if project_path and os.path.exists(project_path):
            if not project.read(str(project_path)) and not project.mapLayers():
                _emit(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"could not read project file: {project_path}",
                        mode=ExecutionMode.HEADLESS,
                        project_path=project_path,
                    )
                )
                return 1
        else:
            project.clear()
            created = True
            if project_path:
                # Relative layer paths, so a project created here stays
                # portable -- the same reason the fixture project uses them.
                project.setFilePathStorage(Qgis.FilePathType.Relative)

        layer = QgsRasterLayer(str(layer_path), str(layer_name))
        if not layer.isValid():
            _emit(
                error(
                    ErrorCode.BAD_REQUEST,
                    f"the acquired file did not load as a raster layer: "
                    f"{layer_path}. It may be incomplete or in an unexpected "
                    f"format.",
                    mode=ExecutionMode.HEADLESS,
                    layer_path=layer_path,
                )
            )
            return 1

        # Provenance, written before the layer joins the project so that a
        # failure part-way cannot leave a registered layer with no record of
        # where it came from.
        recorded = {}
        for field in PROVENANCE_FIELDS:
            value = provenance.get(field)
            if value is None:
                continue
            text = (
                json.dumps(value)
                if isinstance(value, (list, dict, bool))
                else str(value)
            )
            layer.setCustomProperty(f"{PROPERTY_PREFIX}{field}", text)
            recorded[field] = value

        # Also into the layer's own metadata, which is what QGIS surfaces in
        # the layer properties dialogue -- a person looking at this project in
        # six months finds it there rather than in custom properties.
        metadata = layer.metadata()
        attribution = provenance.get("attribution") or ""
        datum = provenance.get("vertical_datum") or "unknown"
        summary = [
            f"Acquired by qgis-mcp from {provenance.get('provider', 'unknown')}",
            f"Dataset: {provenance.get('dataset_identifier', 'unknown')}",
            f"Vertical datum: {datum}",
        ]
        if attribution:
            summary.append(f"Attribution: {attribution}")
        metadata.setAbstract("\n".join(summary))
        if attribution:
            metadata.setRights([attribution])
        layer.setMetadata(metadata)

        project.addMapLayer(layer)

        written_project = None
        if project_path:
            if not project.write(str(project_path)):
                _emit(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"could not write the project to {project_path}",
                        mode=ExecutionMode.HEADLESS,
                        project_path=project_path,
                    )
                )
                return 1
            written_project = str(project_path)

        result = {
            "layer_id": layer.id(),
            "layer_name": layer.name(),
            "layer_path": str(layer_path),
            "crs": layer.crs().authid(),
            "width": layer.width(),
            "height": layer.height(),
            "band_count": layer.bandCount(),
            "project_path": written_project,
            "project_created": created,
            "project_layer_count": len(project.mapLayers()),
            "provenance_recorded": recorded,
            "provenance_property_prefix": PROPERTY_PREFIX,
        }
        project.clear()
        _emit(ok(result, mode=ExecutionMode.HEADLESS))
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        _emit(
            error(
                ErrorCode.CHILD_FAILED,
                f"{type(exc).__name__}: {exc}",
                mode=ExecutionMode.HEADLESS,
                traceback=traceback.format_exc(),
            )
        )
        return 1
    finally:
        app.exitQgis()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
