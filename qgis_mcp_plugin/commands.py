"""The live listener's command handlers (tasks 6.3 and 6.4).

**Every function here runs on the QGIS main thread.** The listener hands each
one to the marshaller rather than calling it, so touching the canvas, the layer
tree or the live project is safe in this file and only in this file. Nothing
here should ever be called directly from the socket thread.

Imports go through ``qgis.PyQt`` (``design.md`` §6.2), never ``PyQt5`` or
``PyQt6`` directly, so the source works on either binding.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import tempfile
import traceback
from typing import Any, Callable

from qgis.core import Qgis, QgsProject, QgsRectangle
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice

from .backups import DEFAULT_RETENTION, BackupError, list_backups, take_backup
from .errors import CodeRaised, CommandError, InterfaceUnavailable

#: REQ-5's cap on a returned screenshot, in bytes.
#:
#: Defaults below a typical MCP message limit with room for the rest of the
#: response. A PNG that overruns is re-encoded smaller rather than truncated,
#: since half a PNG is not an image.
DEFAULT_SCREENSHOT_BYTE_CAP = 900_000

#: Smallest width the screenshot downscaler will settle for before giving up.
MIN_SCREENSHOT_WIDTH = 320

#: REQ-3's return convention, shared with the headless runner.
RESULT_NAME = "result"


def _iface():
    """The QGIS interface, or a clear failure.

    ``qgis.utils.iface`` exists only inside a running QGIS. Reaching for it in
    a headless harness should say so rather than raise ``AttributeError`` from
    somewhere deeper.
    """
    try:
        from qgis.utils import iface
    except ImportError as exc:  # pragma: no cover - only outside QGIS
        raise InterfaceUnavailable(
            "the QGIS interface could not be imported, so this command needs "
            "a running QGIS session"
        ) from exc
    if iface is None:
        raise InterfaceUnavailable(
            "this command needs the QGIS desktop interface, and there is none "
            "in this process. Canvas and window operations require the "
            "application itself; every other command works headless."
        )
    return iface


def _layer_by_name(name: str):
    """Resolve a layer name, returning candidates when it does not match.

    The same "return the candidates, never fail bare" shape as the rest of the
    server (``requirements.md`` §0). A wrong layer name is the single most
    likely mistake in any of these commands.
    """
    if not name:
        raise CommandError("layer is required")
    project = QgsProject.instance()
    matches = project.mapLayersByName(name)
    if len(matches) == 1:
        return matches[0]
    available = sorted(layer.name() for layer in project.mapLayers().values())
    if not matches:
        raise CommandError(
            f"no layer named {name!r} in the open project. Available: "
            f"{', '.join(available) or '(none)'}"
        )
    raise CommandError(
        f"{len(matches)} layers are named {name!r}, so this is ambiguous. "
        f"Rename one, or act on it through execute_pyqgis_code by id."
    )


# ---------------------------------------------------------------------------
# Liveness and state
# ---------------------------------------------------------------------------


def ping(params: dict[str, Any]) -> dict[str, Any]:
    """Confirm the listener is alive and say what it is attached to.

    The first thing the server calls, and the probe behind
    ``modes.live_is_reachable``. It deliberately touches ``QgsProject`` rather
    than returning a constant: a reply proves not just that the socket answered
    but that the main-thread boundary works and the project is reachable.
    """
    import threading

    project = QgsProject.instance()
    return {
        "alive": True,
        "qgis_version": Qgis.QGIS_VERSION,
        "qgis_version_int": Qgis.QGIS_VERSION_INT,
        "project_file": project.fileName() or None,
        "project_title": project.title() or None,
        "layer_count": len(project.mapLayers()),
        "has_unsaved_changes": bool(project.isDirty()),
        # Returned so a caller can confirm the marshalling actually happened
        # rather than the handler having run on the socket thread.
        "executed_on_thread": threading.get_ident(),
        "interface_available": _interface_available(),
    }


def _interface_available() -> bool:
    try:
        from qgis.utils import iface
    except ImportError:
        return False
    return iface is not None


def project_state(params: dict[str, Any]) -> dict[str, Any]:
    """The live project's state, including what is unsaved.

    The one thing the headless path cannot answer: whether the session has
    edits that are not on disk. REQ-1b's reported mode is what tells a caller
    which answer they got, and this is the difference it makes.
    """
    project = QgsProject.instance()
    layers = []
    for layer in project.mapLayers().values():
        layers.append(
            {
                "name": layer.name(),
                "id": layer.id(),
                "type": layer.type().name,
                "is_valid": layer.isValid(),
                "crs": layer.crs().authid(),
                "visible": _is_visible(project, layer),
            }
        )
    layers.sort(key=lambda entry: entry["name"])

    active = None
    with contextlib.suppress(CommandError):
        current = _iface().activeLayer()
        if current is not None:
            active = {"name": current.name(), "id": current.id()}

    return {
        "project_file": project.fileName() or None,
        "project_title": project.title() or None,
        "crs": project.crs().authid(),
        "has_unsaved_changes": bool(project.isDirty()),
        "layer_count": len(layers),
        "layers": layers,
        # Available here and only here: the active layer is an interface
        # concept, which is why the headless path reports it as unavailable
        # rather than null.
        "active_layer": active,
        "layouts": [item.name() for item in project.layoutManager().layouts()],
    }


def _is_visible(project, layer) -> bool | None:
    node = project.layerTreeRoot().findLayer(layer.id())
    return None if node is None else bool(node.itemVisibilityChecked())


# ---------------------------------------------------------------------------
# 6.3 execute_pyqgis_code, with the REQ-4a backup
# ---------------------------------------------------------------------------


def execute_pyqgis_code(params: dict[str, Any]) -> dict[str, Any]:
    """Run agent-authored code against the live session (REQ-3, REQ-4a).

    A backup is taken **before** execution and its path returned, which is what
    REQ-4a requires in place of REQ-4's confirmation gate. If the backup cannot
    be written the code does not run: the backup is the only thing standing in
    for the confirmation that was skipped, so proceeding without one would
    leave the operation entirely unprotected.
    """
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CommandError("code must be a non-empty string")

    try:
        compiled = compile(code, "<agent-code>", "exec")
    except SyntaxError as exc:
        # Refused before the backup: a syntax error cannot modify anything, so
        # taking a snapshot for it would just churn the retention window.
        raise CommandError(
            f"submitted code has a syntax error: {exc.msg} (line "
            f"{exc.lineno}, offset {exc.offset})"
        ) from exc

    project = QgsProject.instance()
    retention = params.get("backup_retention")
    try:
        backup = take_backup(
            project,
            retention=int(retention) if retention else DEFAULT_RETENTION,
        )
    except BackupError as exc:
        raise CommandError(
            f"refusing to execute: the pre-execution backup failed ({exc}). "
            f"REQ-4a makes that backup the reason raw execution skips the "
            f"confirmation gate, so running without one would leave this "
            f"unprotected."
        ) from exc

    namespace: dict[str, Any] = {
        "__name__": "__agent__",
        "__builtins__": __builtins__,
        # Handed in so the common case needs no imports, and so code that
        # wants the interface gets the real one rather than reaching for a
        # module-level global.
        "qgis_project": project,
    }
    with contextlib.suppress(CommandError):
        namespace["iface"] = _iface()

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            exec(compiled, namespace)
    except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
        raise CodeRaised(
            f"{type(exc).__name__}: {exc}",
            backup=backup,
            stdout=captured.getvalue(),
            traceback_text=traceback.format_exc(),
            exception_type=type(exc).__name__,
        ) from exc

    stdout = captured.getvalue()
    result: dict[str, Any] = {
        "backup": backup,
        "result_defined": RESULT_NAME in namespace,
        "result": namespace.get(RESULT_NAME),
        "has_unsaved_changes": bool(project.isDirty()),
        "layer_count": len(project.mapLayers()),
    }
    if stdout:
        result["stdout"] = stdout

    good, detail = _json_safe(result["result"])
    if not good:
        raise CommandError(
            f"the code ran, but {detail} REQ-3 requires {RESULT_NAME} to hold "
            f"plain data. The backup is at {backup['backup_path']}."
        )
    return result


def _json_safe(value, *, path: str = RESULT_NAME):
    """Check a value will cross the socket, per REQ-3.

    The same contract the headless runner enforces, named the same way: the
    offending *path* rather than only its type, because the usual failure is
    one PyQGIS object inside an otherwise fine dict.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return True, ""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return False, f"{path} is the non-JSON float {value!r}."
        return True, ""
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            good, detail = _json_safe(item, path=f"{path}[{index}]")
            if not good:
                return False, detail
        return True, ""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return False, (
                    f"{path} has a non-string key {key!r}; JSON object keys "
                    f"must be strings."
                )
            good, detail = _json_safe(item, path=f"{path}[{key!r}]")
            if not good:
                return False, detail
        return True, ""
    return False, (
        f"{path} is a {type(value).__name__}, which is not JSON-serialisable. "
        f"Convert PyQGIS objects to values first (layer.name(), "
        f"feature.attributes(), geometry.asWkt())."
    )


def project_backups(params: dict[str, Any]) -> dict[str, Any]:
    """List the backups on disk.

    REQ-4c defers ``restore_project_backup`` to the write-operations phase, so
    recovery is manual: open the ``.qgz``. This is what makes that findable.
    """
    entries = list_backups()
    return {
        "count": len(entries),
        "backups": entries[: int(params.get("limit") or 50)],
        "restore": (
            "recovery is manual in this build: open the .qgz in QGIS. Note a "
            "backup restores project structure only, not underlying data."
        ),
    }


# ---------------------------------------------------------------------------
# 6.4 Screenshots and view control
# ---------------------------------------------------------------------------


def _png_within_cap(image, cap: int) -> tuple[bytes, dict[str, Any]]:
    """Encode an image as PNG, shrinking until it fits ``cap`` bytes (REQ-5).

    Downscaled rather than truncated, because a truncated PNG is not a smaller
    picture, it is a corrupt file. Each step halves the width, which converges
    in a handful of iterations for any plausible canvas.
    """
    from qgis.PyQt.QtCore import Qt as QtCore_Qt

    width = image.width()
    attempts = 0
    current = image
    while True:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        current.save(buffer, "PNG")
        data = bytes(buffer.data())
        buffer.close()
        if len(data) <= cap or current.width() <= MIN_SCREENSHOT_WIDTH:
            return data, {
                "width": current.width(),
                "height": current.height(),
                "bytes": len(data),
                "downscaled": attempts > 0,
                "downscale_steps": attempts,
                "byte_cap": cap,
                "original_width": width,
            }
        attempts += 1
        current = current.scaledToWidth(
            max(MIN_SCREENSHOT_WIDTH, current.width() // 2),
            QtCore_Qt.TransformationMode.SmoothTransformation,
        )


def get_screenshot_of_canvas(params: dict[str, Any]) -> dict[str, Any]:
    """Render the current map canvas to a PNG (REQ-5).

    Live-only by nature: a canvas view exists in the GUI and nowhere else,
    which is why REQ-1a lists this tool and why the headless path refuses it
    rather than substituting a render.
    """
    canvas = _iface().mapCanvas()
    cap = int(params.get("byte_cap") or DEFAULT_SCREENSHOT_BYTE_CAP)
    if cap < 10_000:
        raise CommandError(
            f"byte_cap of {cap} is too small for a usable PNG; the minimum is "
            f"10000."
        )

    pixmap = canvas.grab()
    data, meta = _png_within_cap(pixmap.toImage(), cap)
    extent = canvas.extent()
    return {
        "format": "png",
        "image_base64": base64.b64encode(data).decode("ascii"),
        **meta,
        "extent": [
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
        ],
        "extent_crs": canvas.mapSettings().destinationCrs().authid(),
        "scale": canvas.scale(),
    }


def get_screenshot_of_window(params: dict[str, Any]) -> dict[str, Any]:
    """Grab the whole QGIS window, for layout and dialog state."""
    window = _iface().mainWindow()
    cap = int(params.get("byte_cap") or DEFAULT_SCREENSHOT_BYTE_CAP)
    pixmap = window.grab()
    data, meta = _png_within_cap(pixmap.toImage(), cap)
    return {
        "format": "png",
        "image_base64": base64.b64encode(data).decode("ascii"),
        **meta,
        "window_title": window.windowTitle(),
    }


def zoom_to_layer(params: dict[str, Any]) -> dict[str, Any]:
    """Fit the canvas to a named layer's extent."""
    layer = _layer_by_name(str(params.get("layer") or ""))
    canvas = _iface().mapCanvas()
    extent = layer.extent()
    if extent.isNull() or extent.isEmpty():
        raise CommandError(
            f"{layer.name()!r} has no extent to zoom to -- it may be empty or "
            f"its source may be unavailable."
        )

    # Transformed into the canvas CRS before zooming. Without this, zooming to
    # a layer whose CRS differs from the project's lands somewhere else
    # entirely while reporting success.
    from qgis.core import QgsCoordinateTransform

    destination = canvas.mapSettings().destinationCrs()
    if layer.crs().isValid() and destination.isValid() and layer.crs() != destination:
        transform = QgsCoordinateTransform(
            layer.crs(), destination, QgsProject.instance().transformContext()
        )
        extent = transform.transformBoundingBox(extent)

    margin = float(params.get("margin_fraction") or 0.05)
    if margin > 0:
        extent = QgsRectangle(extent)
        extent.scale(1.0 + margin)

    canvas.setExtent(extent)
    canvas.refresh()
    return {
        "layer": layer.name(),
        "extent": [
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
        ],
        "extent_crs": destination.authid(),
        "scale": canvas.scale(),
        "layer_crs": layer.crs().authid(),
        "transformed": layer.crs() != destination,
    }


def set_active_layer(params: dict[str, Any]) -> dict[str, Any]:
    """Select a layer in the Layers panel."""
    layer = _layer_by_name(str(params.get("layer") or ""))
    interface = _iface()
    previous = interface.activeLayer()
    interface.setActiveLayer(layer)
    return {
        "active_layer": layer.name(),
        "previous_active_layer": previous.name() if previous is not None else None,
    }


def toggle_layer_visibility(params: dict[str, Any]) -> dict[str, Any]:
    """Show or hide a named layer or group."""
    name = str(params.get("layer") or "")
    if not name:
        raise CommandError("layer is required")
    requested = params.get("visible")

    project = QgsProject.instance()
    root = project.layerTreeRoot()

    node = None
    for candidate in project.mapLayersByName(name):
        node = root.findLayer(candidate.id())
        if node is not None:
            break
    if node is None:
        # Groups are named in the tree rather than in mapLayers, so a group
        # name would otherwise look like a missing layer.
        node = root.findGroup(name)
    if node is None:
        available = sorted(layer.name() for layer in project.mapLayers().values())
        groups = [child.name() for child in root.children() if hasattr(child, "findLayer")]
        raise CommandError(
            f"no layer or group named {name!r}. Layers: "
            f"{', '.join(available) or '(none)'}. Groups: "
            f"{', '.join(groups) or '(none)'}."
        )

    before = bool(node.itemVisibilityChecked())
    target = (not before) if requested is None else bool(requested)
    node.setItemVisibilityChecked(target)

    # Best-effort, and deliberately after the change rather than guarding it.
    # The layer tree is the real state here; the refresh only repaints. Letting
    # a missing canvas raise would report failure for a change that had already
    # been applied -- an effect without an acknowledgement, which is worse than
    # either a clean success or a clean refusal.
    refreshed = True
    try:
        _iface().mapCanvas().refresh()
    except InterfaceUnavailable:
        refreshed = False

    return {
        "name": name,
        "was_visible": before,
        "visible": target,
        "toggled": requested is None,
        "canvas_refreshed": refreshed,
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_handlers() -> dict[str, Callable[[dict[str, Any]], Any]]:
    """Every command the listener serves.

    Assembled as data so the listener knows nothing about any individual
    command, and an unknown name can be answered with the full list.
    """
    return {
        "ping": ping,
        "project_state": project_state,
        "execute_pyqgis_code": execute_pyqgis_code,
        "project_backups": project_backups,
        "get_screenshot_of_canvas": get_screenshot_of_canvas,
        "get_screenshot_of_window": get_screenshot_of_window,
        "zoom_to_layer": zoom_to_layer,
        "set_active_layer": set_active_layer,
        "toggle_layer_visibility": toggle_layer_visibility,
    }


__all__ = [
    "DEFAULT_SCREENSHOT_BYTE_CAP",
    "build_handlers",
    "execute_pyqgis_code",
    "get_screenshot_of_canvas",
    "get_screenshot_of_window",
    "ping",
    "project_backups",
    "project_state",
    "set_active_layer",
    "toggle_layer_visibility",
    "zoom_to_layer",
]
