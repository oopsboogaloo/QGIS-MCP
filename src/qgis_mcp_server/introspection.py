"""Read-only project and layer introspection (task 2).

A thin server-side front for ``_child/introspect.py``: validate what can be
validated without starting QGIS, then hand the query across the process
boundary. The PyQGIS lives in the child; what lives here is the part worth doing
before paying a two-second startup.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, ExecutionMode, error
from qgis_mcp_server.headless import INTROSPECT_SCRIPT, HeadlessRunner

#: Operations the child answers. Mirrors ``_child/introspect.py``'s own list;
#: checked on both sides so a typo is caught here, before a QGIS startup, and
#: the child still refuses anything it does not recognise.
OPERATIONS = (
    "project_summary",
    "layer_summary",
    "layers_tree",
    "missing_layers",
    "project_path_info",
)


def query(
    headless: HeadlessRunner,
    operation: str,
    *,
    project_path: Path | str | None = None,
    timeout: float | None = None,
    **parameters: Any,
) -> Envelope:
    """Run one introspection operation and return its envelope."""
    if operation not in OPERATIONS:
        return error(
            ErrorCode.BAD_REQUEST,
            f"unknown introspection operation {operation!r}",
            mode=ExecutionMode.HEADLESS,
            supported_operations=list(OPERATIONS),
        )

    resolved, failure = headless.resolve_project(project_path)
    if failure is not None:
        return failure
    if resolved is None:
        # Checked here so the common misconfiguration costs nothing. REQ-2
        # makes the project path the headless path's input, and reporting an
        # empty project as though it were real would be worse than refusing.
        return error(
            ErrorCode.BAD_REQUEST,
            f"{operation} reports on a project, and none is available. Pass "
            f"project_path, or configure one with --project or the [project] "
            f"path key, so there is something to report on.",
            mode=ExecutionMode.HEADLESS,
            operation=operation,
        )

    request: dict[str, Any] = {
        "operation": operation,
        "project_path": str(resolved),
    }
    request.update({key: value for key, value in parameters.items() if value is not None})

    return headless.run_child(
        INTROSPECT_SCRIPT,
        request,
        timeout=timeout,
        timeout_hint=(
            "Introspection reads layer metadata, which can be slow for a "
            "project with many large remote layers."
        ),
    )


__all__ = ["OPERATIONS", "query"]
