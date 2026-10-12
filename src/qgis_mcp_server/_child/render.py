"""Map and layout rendering (tasks 5.3 and 5.6).

Executes in the **child** process under the QGIS-bundled interpreter. Headless
rendering works: ``QgsMapRendererSequentialJob`` paints into a ``QImage``, which
is a raster paint device and needs no display, so ``QgsApplication([], False)``
is enough.

Two operations, sharing the machinery the spec says they should:

``render_layout`` (5.6) exports a named Print Layout to PNG or PDF. This is the
half of ``render_map_to_path`` that is mode-agnostic under REQ-1b -- a Print
Layout is a project object, so rendering one needs no canvas and no GUI. Only
the *canvas* variant requires a live session, which is why REQ-1a pointedly
excludes this tool.

``render_map`` (5.3) renders a chosen combination of layers over a chosen extent
and returns the world-space bounding box, so the image can be UV-aligned
against a heightmap-derived mesh.

**The extent returned is the extent rendered, which is not always the one
asked for.** ``QgsMapSettings`` expands the extent to match the output image's
aspect ratio. Asking for a 1000 m square at 256x200 px really renders 1280 m by
1000 m -- verified, not assumed. A texture UV-mapped to the *requested* box
would then be misaligned by 140 m on each side, with nothing in the image to
show it. So the output size is derived from the extent wherever possible, and
``visibleExtent()`` is what gets reported either way.

Usage: ``python render.py <request.json>``
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

OPERATIONS = ("render_layout", "render_map")

#: Raster formats for a map render. PNG for anything with sharp edges or
#: transparency; JPEG only where file size beats fidelity.
IMAGE_FORMATS = ("png", "jpg", "tif")

LAYOUT_FORMATS = ("png", "pdf", "svg", "jpg")

DEFAULT_DPI = 300.0
DEFAULT_IMAGE_WIDTH = 1024

#: Sampling stride for the blank-image check. REQ-T4 asks for "non-blank" as a
#: mechanical assertion, and sampling a grid is enough to distinguish a real
#: render from an empty canvas without reading every pixel of a large image.
BLANK_CHECK_STRIDE = 7


def _emit(envelope) -> None:
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def _fail(code: str, message: str, **detail):
    _emit(error(code, message, mode=ExecutionMode.HEADLESS, **detail))


def _number(request: dict, key: str, default: float) -> float:
    """Read a numeric request field, treating only absence as absent.

    ``request.get(key) or default`` is wrong for numbers and the failure is
    silent: 0 is falsy, so a caller passing ``dpi=0`` gets the default instead
    of the validation error that would tell them 0 is not a DPI. Explicitly
    checking for None is the whole fix.
    """
    value = request.get(key)
    return default if value is None else float(value)


class _Refusal(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _distinct_colours(image) -> int:
    """Rough count of distinct sampled colours, to tell a render from a blank."""
    width, height = image.width(), image.height()
    if width == 0 or height == 0:
        return 0
    return len(
        {
            image.pixel(x, y)
            for x in range(0, width, BLANK_CHECK_STRIDE)
            for y in range(0, height, BLANK_CHECK_STRIDE)
        }
    )


def _write_world_file(path: str, extent, width: int, height: int) -> str | None:
    """Write an ESRI world file so the image is georeferenced on disk.

    A world file's last two values are the coordinates of the **centre** of the
    upper-left pixel, not its corner -- a half-pixel offset that is easy to get
    wrong and shifts every subsequent alignment by half a cell.
    """
    stem, extension = os.path.splitext(path)
    sidecar = {".png": ".pgw", ".jpg": ".jgw", ".jpeg": ".jgw", ".tif": ".tfw"}.get(
        extension.lower()
    )
    if sidecar is None:
        return None

    pixel_width = (extent.xMaximum() - extent.xMinimum()) / width
    pixel_height = (extent.yMaximum() - extent.yMinimum()) / height
    lines = [
        f"{pixel_width:.12f}",
        "0.0",
        "0.0",
        f"{-pixel_height:.12f}",
        f"{extent.xMinimum() + pixel_width / 2.0:.6f}",
        f"{extent.yMaximum() - pixel_height / 2.0:.6f}",
    ]
    destination = stem + sidecar
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return destination


def _resolve_layers(project, requested):
    """Resolve layer names to layers, in the order given.

    Order is draw order, and it matters: the first entry ends up on top. An
    unknown name is refused with the available names rather than silently
    dropped -- a texture quietly missing its geology overlay looks like a
    styling problem, not a typo.
    """
    available = {layer.name(): layer for layer in project.mapLayers().values()}
    if not requested:
        # The project's own layer tree order, so an unqualified render matches
        # what the project looks like rather than dictionary order.
        ordered = []
        for node in project.layerTreeRoot().findLayers():
            layer = node.layer()
            if layer is not None and layer.isValid():
                ordered.append(layer)
        return ordered or [layer for layer in available.values() if layer.isValid()]

    resolved = []
    missing = []
    for name in requested:
        layer = available.get(name)
        if layer is None:
            missing.append(name)
        else:
            resolved.append(layer)
    if missing:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"no layer named {', '.join(repr(name) for name in missing)} in "
            f"this project.",
            missing_layers=missing,
            available_layers=sorted(available),
        )
    return resolved


# ---------------------------------------------------------------------------
# 5.3 render_map
# ---------------------------------------------------------------------------


def op_render_map(request: dict, project) -> dict:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsMapRendererSequentialJob,
        QgsMapSettings,
        QgsRectangle,
    )
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor

    destination = request["destination"]
    fmt = (request.get("format") or "png").lower()
    if fmt not in IMAGE_FORMATS:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"format must be one of {IMAGE_FORMATS}; got {fmt!r}",
        )

    layers = _resolve_layers(project, request.get("layers"))
    if not layers:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            "this project has no valid layers to render.",
        )

    target_crs = project.crs()
    if request.get("crs"):
        target_crs = QgsCoordinateReferenceSystem(str(request["crs"]))
        if not target_crs.isValid():
            raise _Refusal(
                ErrorCode.BAD_REQUEST, f"crs is not usable: {request['crs']!r}"
            )

    # --- extent -------------------------------------------------------------
    bbox = request.get("extent")
    extent_crs = request.get("extent_crs")
    if bbox:
        if len(bbox) != 4:
            raise _Refusal(
                ErrorCode.BAD_REQUEST,
                f"extent must be [xmin, ymin, xmax, ymax]; got {bbox!r}",
            )
        requested = QgsRectangle(*(float(value) for value in bbox))
        if extent_crs and str(extent_crs) != target_crs.authid():
            source = QgsCoordinateReferenceSystem(str(extent_crs))
            if not source.isValid():
                raise _Refusal(
                    ErrorCode.BAD_REQUEST,
                    f"extent_crs is not usable: {extent_crs!r}",
                )
            transform = QgsCoordinateTransform(
                source, target_crs, project.transformContext()
            )
            requested = transform.transformBoundingBox(requested)
    else:
        # The union of the layers being rendered, which is what "render this"
        # means with no extent given.
        requested = QgsRectangle()
        requested.setNull()
        for layer in layers:
            layer_extent = layer.extent()
            if layer_extent.isNull() or layer_extent.isEmpty():
                continue
            if layer.crs().isValid() and layer.crs() != target_crs:
                transform = QgsCoordinateTransform(
                    layer.crs(), target_crs, project.transformContext()
                )
                layer_extent = transform.transformBoundingBox(layer_extent)
            requested.combineExtentWith(layer_extent)
        if requested.isNull() or requested.isEmpty():
            raise _Refusal(
                ErrorCode.BAD_REQUEST,
                "the layers have no usable extent, so there is nothing to "
                "frame the render on. Pass an explicit extent.",
            )

    extent_width = requested.xMaximum() - requested.xMinimum()
    extent_height = requested.yMaximum() - requested.yMinimum()
    if extent_width <= 0 or extent_height <= 0:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"the extent has zero width or height: "
            f"{[requested.xMinimum(), requested.yMinimum(), requested.xMaximum(), requested.yMaximum()]}",
        )

    # --- output size, derived so the extent is not adjusted -----------------
    pixel_size = request.get("pixel_size")
    width = request.get("width")
    height = request.get("height")
    derived_from = None

    if pixel_size:
        pixel_size = float(pixel_size)
        if pixel_size <= 0:
            raise _Refusal(ErrorCode.BAD_REQUEST, "pixel_size must be positive")
        width = max(1, round(extent_width / pixel_size))
        height = max(1, round(extent_height / pixel_size))
        derived_from = "pixel_size"
    elif width and not height:
        width = int(width)
        height = max(1, round(width * extent_height / extent_width))
        derived_from = "width"
    elif height and not width:
        height = int(height)
        width = max(1, round(height * extent_width / extent_height))
        derived_from = "height"
    elif width and height:
        width, height = int(width), int(height)
        derived_from = "both"
    else:
        width = DEFAULT_IMAGE_WIDTH
        height = max(1, round(width * extent_height / extent_width))
        derived_from = "default_width"

    if width < 1 or height < 1 or width > 30000 or height > 30000:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"output size {width}x{height} is outside the usable range "
            f"1..30000 per side.",
        )

    settings = QgsMapSettings()
    settings.setLayers(layers)
    settings.setDestinationCrs(target_crs)
    settings.setOutputSize(QSize(width, height))
    settings.setExtent(requested)
    settings.setOutputDpi(_number(request, "dpi", 96.0))

    background = request.get("background")
    transparent = bool(request.get("transparent", False))
    if transparent:
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
    else:
        settings.setBackgroundColor(QColor(background or "#ffffff"))

    # The extent actually rendered. With a derived size this equals what was
    # asked for; with a mismatched width and height it does not, and this is
    # the only honest answer to "what ground does this image cover".
    visible = settings.visibleExtent()
    adjusted = not (
        abs(visible.xMinimum() - requested.xMinimum()) < 1e-6
        and abs(visible.yMinimum() - requested.yMinimum()) < 1e-6
        and abs(visible.xMaximum() - requested.xMaximum()) < 1e-6
        and abs(visible.yMaximum() - requested.yMaximum()) < 1e-6
    )

    job = QgsMapRendererSequentialJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()

    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    if not image.save(destination):
        raise _Refusal(
            ErrorCode.CHILD_FAILED,
            f"the map rendered but the image could not be written to "
            f"{destination}.",
        )

    world_file = _write_world_file(destination, visible, width, height)
    colours = _distinct_colours(image)

    result = {
        "path": destination,
        "format": fmt,
        "width": image.width(),
        "height": image.height(),
        "file_bytes": os.path.getsize(destination),
        "layers": [layer.name() for layer in layers],
        "crs": target_crs.authid(),
        # The REQ-5.3 payload: the world-space box this image actually covers.
        "extent": [
            visible.xMinimum(),
            visible.yMinimum(),
            visible.xMaximum(),
            visible.yMaximum(),
        ],
        "extent_crs": target_crs.authid(),
        "requested_extent": [
            requested.xMinimum(),
            requested.yMinimum(),
            requested.xMaximum(),
            requested.yMaximum(),
        ],
        "extent_was_adjusted": adjusted,
        "ground_width_m": visible.xMaximum() - visible.xMinimum(),
        "ground_height_m": visible.yMaximum() - visible.yMinimum(),
        "metres_per_pixel_x": (visible.xMaximum() - visible.xMinimum()) / width,
        "metres_per_pixel_y": (visible.yMaximum() - visible.yMinimum()) / height,
        "size_derived_from": derived_from,
        "world_file": world_file,
        "distinct_colours_sampled": colours,
        "is_blank": colours <= 1,
        "warnings": [],
    }

    if adjusted:
        result["warnings"].append(
            "the extent was expanded to match the image's aspect ratio, so "
            "this image covers more ground than requested. UV-align against "
            "'extent', not 'requested_extent'. To avoid the expansion, pass "
            "pixel_size, or pass only one of width and height."
        )
    if colours <= 1:
        result["warnings"].append(
            "the render is a single flat colour, so nothing drew. Check the "
            "extent overlaps the layers and that the layers are visible."
        )
    if target_crs.isGeographic():
        result["warnings"].append(
            f"the render CRS is geographic ({target_crs.authid()}), so "
            f"metres_per_pixel is in degrees and the image is stretched "
            f"relative to the ground. Render in a projected CRS for anything "
            f"UV-aligned against a mesh."
        )
    return result


# ---------------------------------------------------------------------------
# 5.6 render_layout
# ---------------------------------------------------------------------------


def op_render_layout(request: dict, project) -> dict:
    from qgis.core import QgsLayoutExporter

    destination = request["destination"]
    fmt = (request.get("format") or "png").lower()
    dpi = _number(request, "dpi", DEFAULT_DPI)
    name = request.get("layout")

    if fmt not in LAYOUT_FORMATS:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"format must be one of {LAYOUT_FORMATS}; got {fmt!r}",
        )
    if dpi <= 0 or dpi > 2400:
        raise _Refusal(
            ErrorCode.BAD_REQUEST, f"dpi must be between 1 and 2400; got {dpi}"
        )

    manager = project.layoutManager()
    layouts = manager.layouts()
    available = [item.name() for item in layouts]

    if name:
        layout = manager.layoutByName(str(name))
        if layout is None:
            raise _Refusal(
                ErrorCode.BAD_REQUEST,
                f"no layout named {name!r} in this project.",
                available_layouts=available,
            )
    elif len(layouts) == 1:
        layout = layouts[0]
    elif not layouts:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            "this project has no print layouts. Create one in QGIS, or use a "
            "map render instead of a layout export.",
            available_layouts=[],
        )
    else:
        # Picking one would be a guess the caller cannot detect.
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"this project has {len(layouts)} layouts, so name the one you "
            f"want.",
            available_layouts=available,
        )

    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    exporter = QgsLayoutExporter(layout)

    if fmt == "pdf":
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.dpi = dpi
        # Georeference the PDF where the layout has a map item, which costs
        # nothing and makes the output usable as a GIS layer rather than only
        # as a picture.
        settings.appendGeoreference = True
        outcome = exporter.exportToPdf(destination, settings)
    elif fmt == "svg":
        settings = QgsLayoutExporter.SvgExportSettings()
        settings.dpi = dpi
        outcome = exporter.exportToSvg(destination, settings)
    else:
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = dpi
        settings.generateWorldFile = bool(request.get("world_file", True))
        outcome = exporter.exportToImage(destination, settings)

    if outcome != QgsLayoutExporter.Success:
        raise _Refusal(
            ErrorCode.CHILD_FAILED,
            f"QGIS could not export the layout (result code {outcome}). The "
            f"destination may not be writable, or the layout may reference a "
            f"missing layer.",
            layout=layout.name(),
            destination=destination,
        )

    page = layout.pageCollection().page(0)
    page_size = page.pageSize() if page is not None else None

    result = {
        "path": destination,
        "format": fmt,
        "layout": layout.name(),
        "dpi": dpi,
        "file_bytes": os.path.getsize(destination) if os.path.exists(destination) else 0,
        "available_layouts": available,
        "page_width_mm": page_size.width() if page_size is not None else None,
        "page_height_mm": page_size.height() if page_size is not None else None,
        "warnings": [],
    }

    if fmt in ("png", "jpg"):
        from qgis.PyQt.QtGui import QImage

        image = QImage(destination)
        colours = _distinct_colours(image)
        result["width"] = image.width()
        result["height"] = image.height()
        result["distinct_colours_sampled"] = colours
        result["is_blank"] = colours <= 1
        if colours <= 1:
            result["warnings"].append(
                "the exported image is a single flat colour, so the layout's "
                "map item may have no layers or an extent that misses them."
            )
    return result


HANDLERS = {"render_map": op_render_map, "render_layout": op_render_layout}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _fail(ErrorCode.BAD_REQUEST, "render expects one argument, a request file")
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _fail(ErrorCode.BAD_REQUEST, f"could not read request file: {exc}")
        return 2

    operation = request.get("operation")
    if operation not in HANDLERS:
        _fail(
            ErrorCode.BAD_REQUEST,
            f"unknown operation {operation!r}",
            supported_operations=list(OPERATIONS),
        )
        return 2

    project_path = request.get("project_path")
    if not project_path or not os.path.exists(project_path):
        _fail(
            ErrorCode.BAD_REQUEST,
            f"rendering needs a project, and none was found at {project_path!r}. "
            f"Pass project_path, or configure one.",
            project_path=project_path,
        )
        return 1

    from qgis.core import QgsApplication, QgsProject

    prefix_path = request.get("prefix_path") or os.environ.get("QGIS_PREFIX_PATH")
    if prefix_path:
        QgsApplication.setPrefixPath(str(prefix_path), True)
    app = QgsApplication([], False)
    app.initQgis()

    try:
        project = QgsProject.instance()
        read_ok = bool(project.read(str(project_path)))
        if not read_ok and not project.mapLayers():
            _fail(
                ErrorCode.BAD_REQUEST,
                f"could not read project file: {project_path}",
                project_path=str(project_path),
            )
            return 1

        result = HANDLERS[operation](request, project)
        invalid = [
            layer.name()
            for layer in project.mapLayers().values()
            if not layer.isValid()
        ]
        if invalid:
            # A render over a project with broken layers succeeds and quietly
            # omits them, which looks like a styling problem rather than a
            # missing file.
            result.setdefault("warnings", []).append(
                f"these layers could not be loaded and are absent from the "
                f"render: {', '.join(sorted(invalid))}"
            )
        project.clear()
    except _Refusal as refusal:
        _fail(refusal.code, str(refusal), operation=operation, **refusal.detail)
        return 1
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        _fail(
            ErrorCode.CHILD_FAILED,
            f"{type(exc).__name__}: {exc}",
            operation=operation,
            traceback=traceback.format_exc(),
        )
        return 1
    finally:
        app.exitQgis()

    _emit(ok(result, mode=ExecutionMode.HEADLESS, operation=operation))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
