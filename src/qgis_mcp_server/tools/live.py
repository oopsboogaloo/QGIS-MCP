"""Tools that run inside a live QGIS session (tasks 6.3 and 6.4).

Every tool here is **mode-named** under REQ-1a: it names the live mode, so when
no session is reachable it returns an error saying exactly that rather than
quietly running headless. The rationale is REQ-1a's own:

    a live-mode call that silently ran headless would execute against a
    project file with no visible effect on the user's screen, which is
    indistinguishable from the tool having failed -- the worst possible
    failure mode for the interactive loop.

``LiveClient`` already produces that error, so these tools are thin: validate,
call, flatten.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_protocol import ErrorCode, error, ok
from qgis_mcp_server import modes
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.tools.result import tool_result

_SESSION_DESCRIPTION = """\
Report whether a live QGIS session is reachable, and what it has open.

Call this when you need to know if the interactive path is available — before
offering to drive the user's QGIS, or to explain why a live tool refused.

A live session means QGIS is running with the MCP listener plugin enabled
(Plugins → QGIS MCP → Start MCP listener). Without it every live tool returns
mode_unavailable, and the headless tools carry on working against the project
file on disk.

The useful field is has_unsaved_changes: it is the one thing no headless tool
can tell you, and it decides whether the file on disk still represents the
user's work.
"""

_EXECUTE_DESCRIPTION = """\
Execute PyQGIS code inside the running QGIS session, against the open project.

Return data by assigning it to `result`. Only JSON-serialisable data comes
back — convert PyQGIS objects to values first. Anything printed is captured.
`iface` and `qgis_project` are already in scope.

LIVE ONLY. If no session is reachable this returns mode_unavailable rather than
running headless, because a change made to a file on disk with no visible
effect on the user's screen is indistinguishable from the tool having failed.
Use execute_pyqgis_code_for_cli for the headless equivalent.

A PROJECT BACKUP IS TAKEN BEFORE YOUR CODE RUNS, and its path is in the
response. That is what lets this tool skip the confirmation other destructive
tools require. If the backup cannot be written, the code does not run.

What the backup covers: project structure — layer references, styling,
layouts, CRS, settings. What it does NOT cover: underlying data. Code that
overwrites a GeoTIFF, truncates a data provider or deletes a shapefile is not
protected and is not reversible.

Work runs on the QGIS main thread, so a long operation freezes the user's
interface for its duration.
"""

_CANVAS_DESCRIPTION = """\
Capture the current map canvas as a PNG.

Live only — a canvas view exists in the GUI and nowhere else. For a render from
a project file without QGIS open, use render_map_to_path instead.

The PNG is capped in size and downscaled to fit rather than truncated, so what
comes back is always a whole image. The response reports whether it was
downscaled and by how much.

Also returns the canvas extent and scale, so you know what you are looking at
rather than inferring it from the picture.
"""

_WINDOW_DESCRIPTION = """\
Capture the whole QGIS window as a PNG, including panels and dialogs.

Use it to see interface state a canvas capture misses: an open attribute table,
a Processing dialog mid-configuration, which panels are visible, an error
dialog the user is looking at.

Live only, and capped the same way as the canvas capture.
"""

_ZOOM_DESCRIPTION = """\
Zoom the canvas to a named layer's extent.

The extent is transformed into the canvas CRS first. Without that, zooming to a
layer whose CRS differs from the project's lands somewhere else entirely while
reporting success.

A small margin is added by default so the layer is not flush against the edge.
"""

_ACTIVE_LAYER_DESCRIPTION = """\
Select a layer in the Layers panel.

Worth knowing: many QGIS operations and Processing defaults act on the active
layer, so setting it changes what a subsequent manual action will affect. The
previously active layer is returned so you can restore it.
"""

_VISIBILITY_DESCRIPTION = """\
Show or hide a named layer or group in the Layers panel.

Accepts a group name as well as a layer name. Omit `visible` to toggle whatever
the current state is; pass it explicitly to set a known state, which is what
you want when building up a specific view.

The response reports the previous state, so a toggle is reversible.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the live-session tools on ``server``."""

    def live_call(tool_name: str, command: str, **params: Any) -> dict[str, Any]:
        """Dispatch one live command, attaching the mode decision.

        Parameters that were not supplied are dropped rather than sent as
        nulls, so the plugin's own defaults apply instead of being overridden
        with None.
        """
        decision = modes.resolve(tool_name)
        envelope = context.live.call(
            command, {key: value for key, value in params.items() if value is not None}
        )
        return tool_result(envelope, decision)

    @server.tool(name="get_live_session", description=_SESSION_DESCRIPTION)
    def get_live_session() -> dict[str, Any]:
        """Report whether a live QGIS session is reachable (REQ-1)."""
        decision = modes.resolve("get_live_session")
        session = context.live.session(force=True)
        if session is None:
            # Not an error: "no session" is a legitimate answer to this
            # question, and the tool exists precisely to ask it. Returning a
            # failure would make "is QGIS open?" indistinguishable from "the
            # check itself broke".
            return tool_result(
                ok(
                    {
                        "live": False,
                        "reason": context.live.last_error,
                        "expected_port": context.live.port,
                        "enabled": context.live.enabled,
                        "remedy": (
                            "open QGIS and choose Plugins -> QGIS MCP -> Start "
                            "MCP listener. Headless tools work regardless."
                        ),
                    }
                ),
                decision,
            )
        return tool_result(ok({"live": True, **session.to_dict()}), decision)

    @server.tool(name="execute_pyqgis_code", description=_EXECUTE_DESCRIPTION)
    def execute_pyqgis_code(
        code: str,
        backup_retention: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run PyQGIS against the live session (REQ-1a, REQ-3, REQ-4a).

        Args:
            code: PyQGIS source. Assign to ``result`` to return data.
            backup_retention: How many backups to keep for this project.
            timeout_seconds: Override the call timeout. The QGIS main thread
                may legitimately be busy for minutes.
        """
        decision = modes.resolve("execute_pyqgis_code")
        if not isinstance(code, str) or not code.strip():
            return tool_result(
                error(ErrorCode.BAD_REQUEST, "code must be a non-empty string"),
                decision,
            )
        params: dict[str, Any] = {"code": code}
        if backup_retention is not None:
            params["backup_retention"] = backup_retention
        envelope = context.live.call(
            "execute_pyqgis_code", params, timeout=timeout_seconds
        )
        return tool_result(envelope, decision)

    @server.tool(
        name="get_screenshot_of_canvas", description=_CANVAS_DESCRIPTION
    )
    def get_screenshot_of_canvas(byte_cap: int | None = None) -> dict[str, Any]:
        """Capture the map canvas (REQ-5).

        Args:
            byte_cap: Maximum PNG size in bytes. The image is downscaled to
                fit rather than truncated.
        """
        return live_call(
            "get_screenshot_of_canvas",
            "get_screenshot_of_canvas",
            byte_cap=byte_cap,
        )

    @server.tool(
        name="get_screenshot_of_window", description=_WINDOW_DESCRIPTION
    )
    def get_screenshot_of_window(byte_cap: int | None = None) -> dict[str, Any]:
        """Capture the whole QGIS window.

        Args:
            byte_cap: Maximum PNG size in bytes.
        """
        return live_call(
            "get_screenshot_of_window",
            "get_screenshot_of_window",
            byte_cap=byte_cap,
        )

    @server.tool(name="zoom_to_layer", description=_ZOOM_DESCRIPTION)
    def zoom_to_layer(
        layer: str, margin_fraction: float | None = None
    ) -> dict[str, Any]:
        """Fit the canvas to a layer's extent.

        Args:
            layer: Layer name as it appears in the project.
            margin_fraction: Extra margin around the extent, as a fraction.
        """
        return live_call(
            "zoom_to_layer",
            "zoom_to_layer",
            layer=layer,
            margin_fraction=margin_fraction,
        )

    @server.tool(name="set_active_layer", description=_ACTIVE_LAYER_DESCRIPTION)
    def set_active_layer(layer: str) -> dict[str, Any]:
        """Select a layer in the Layers panel.

        Args:
            layer: Layer name as it appears in the project.
        """
        return live_call("set_active_layer", "set_active_layer", layer=layer)

    @server.tool(
        name="toggle_layer_visibility", description=_VISIBILITY_DESCRIPTION
    )
    def toggle_layer_visibility(
        layer: str, visible: bool | None = None
    ) -> dict[str, Any]:
        """Show or hide a layer or group.

        Args:
            layer: Layer or group name.
            visible: Target state. Omit to toggle.
        """
        decision = modes.resolve("toggle_layer_visibility")
        params: dict[str, Any] = {"layer": layer}
        # Passed through even when False, which `live_call` would drop: here
        # False means "hide", not "unspecified".
        if visible is not None:
            params["visible"] = bool(visible)
        return tool_result(
            context.live.call("toggle_layer_visibility", params), decision
        )

    @server.tool(
        name="list_project_backups",
        description=(
            "List the project backups taken before live code execution.\n\n"
            "Restoring is manual in this build: open the .qgz in QGIS. A "
            "backup covers project structure only — layer references, "
            "styling, layouts, CRS — not the underlying data files."
        ),
    )
    def list_project_backups(limit: int = 50) -> dict[str, Any]:
        """List backups on disk.

        Args:
            limit: Maximum entries to return, newest first.
        """
        return live_call("list_project_backups", "project_backups", limit=limit)

    return None


__all__ = ["register"]
