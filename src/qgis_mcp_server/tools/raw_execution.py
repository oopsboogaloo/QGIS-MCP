"""Raw PyQGIS execution tools.

Task 1 implements the headless half: ``execute_pyqgis_code_for_cli``. The live
half, ``execute_pyqgis_code``, arrives with the listener plugin in task 6 --
along with the REQ-4a pre-execution backup, which only means something once
there is an in-memory project that can differ from the file on disk.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_server import modes
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.tools.result import tool_result

#: The description the agent reads. It carries three things it cannot infer:
#: the REQ-3 return convention, the REQ-1a refusal to substitute the other
#: execution mode, and the REQ-4c statement of what is *not* protected.
_CLI_DESCRIPTION = """\
Execute PyQGIS code in a standalone, headless QGIS process.

Return data by assigning it to a variable named `result`. Only
JSON-serialisable data comes back -- convert PyQGIS objects to values first
(layer.name(), feature.attributes(), geometry.asWkt()). Anything printed is
captured and returned separately under `stdout`.

This tool is headless-only. If no usable QGIS install is configured it returns
an error naming the missing mode; it will never silently run your code in a
live QGIS session instead.

SAFETY -- read before writing code that modifies anything. Nothing here is
backed up. The project-structure backup that protects the live-session
equivalent is not implemented on this path yet, and it would not cover
underlying data in any case: code that overwrites a GeoTIFF, truncates a data
provider or deletes a shapefile is unprotected and not reversible. Saving the
project from this tool overwrites the project file on disk.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the raw-execution tools on ``server``."""

    @server.tool(name="execute_pyqgis_code_for_cli", description=_CLI_DESCRIPTION)
    def execute_pyqgis_code_for_cli(
        code: str,
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run ``code`` in a headless QGIS child process.

        Args:
            code: PyQGIS source to execute. Assign to ``result`` to return data.
            project_path: Project to open before executing. Defaults to the
                configured project; omit both to run without one.
            timeout_seconds: Override the configured timeout. Raise it for long
                Processing runs over large rasters.
        """
        decision = modes.resolve("execute_pyqgis_code_for_cli")
        envelope = context.headless.run_code(
            code,
            project_path=project_path,
            timeout=timeout_seconds,
        )
        return tool_result(envelope, decision)

    return None


__all__ = ["register"]
