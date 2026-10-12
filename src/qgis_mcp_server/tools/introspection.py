"""Read-only introspection tools (task 2), per the narrative spec §4.2.

All five are mode-agnostic under REQ-1b, which is why task 2 is placed where it
is: read-only and low risk, "the right place to establish the mode-reporting
convention before anything depends on it".
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_server import introspection, modes
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.tools.result import tool_result

_PROJECT_SUMMARY_DESCRIPTION = """\
Summarise a QGIS project: layer counts by type, project CRS, combined extent,
layout count, and how many layers failed to load.

Start here when you do not know what a project contains. The extent is the
union of all valid layers, transformed into the project CRS.

The active layer is a property of the QGIS interface rather than of the project
file, so it is reported only in live mode; headless responses say so rather
than reporting null, which would read as "no layer is active".
"""

_LAYER_SUMMARY_DESCRIPTION = """\
Describe one layer by name: type, CRS, extent, source path, provider, renderer.

Vector layers also report geometry type, feature count and the full field
schema with QGIS type names. Raster layers report dimensions, band count, pixel
data type and the nodata value.

If the name does not match, the available layer names come back rather than a
bare failure. If several layers share the name, their ids come back so you can
disambiguate — layer names are not unique in QGIS.

Note for GeoPackage layers: the `fid` primary key appears in the field schema,
because that is what QGIS itself sees.
"""

_LAYERS_TREE_DESCRIPTION = """\
The layer panel structure, with groups and nesting preserved.

Returns the tree top to bottom as it appears in QGIS, each node carrying its
name, whether it is checked (visible), and for layers the id, type and validity.
Use this rather than a flat layer list when group membership or draw order
matters.
"""

_MISSING_LAYERS_DESCRIPTION = """\
Find layers whose data source cannot be opened — moved or renamed files, or
formats whose provider is unavailable.

Each entry carries the layer name, its recorded source path, and a reason that
distinguishes a file that is absent from one that is present but unreadable.
Those point at different fixes, so they are reported differently.

An empty list means every layer resolved.
"""

_PROJECT_PATH_INFO_DESCRIPTION = """\
Project file path, save state, CRS and timestamps.

Reports the file's location, size and last-modified time, the QGIS version that
last saved it, and whether layer paths are stored relative or absolute —
relative storage is what makes a project portable between machines.

Unsaved-changes state is only meaningful in live mode. Headless, the project was
read moments ago and is never dirty.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the introspection tools on ``server``."""

    def run(tool_name: str, operation: str, **parameters: Any) -> dict[str, Any]:
        """Dispatch one operation, attaching the REQ-1b serving mode."""
        decision = modes.resolve(tool_name)
        envelope = introspection.query(
            context.headless, operation, **parameters
        )
        return tool_result(envelope, decision)

    @server.tool(
        name="get_project_summary", description=_PROJECT_SUMMARY_DESCRIPTION
    )
    def get_project_summary(
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Summarise a project.

        Args:
            project_path: Project to read. Defaults to the configured project.
            timeout_seconds: Override the configured timeout.
        """
        return run(
            "get_project_summary",
            "project_summary",
            project_path=project_path,
            timeout=timeout_seconds,
        )

    @server.tool(name="get_layer_summary", description=_LAYER_SUMMARY_DESCRIPTION)
    def get_layer_summary(
        layer: str,
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Describe one layer.

        Args:
            layer: Layer name as it appears in the project.
            project_path: Project to read. Defaults to the configured project.
            timeout_seconds: Override the configured timeout.
        """
        return run(
            "get_layer_summary",
            "layer_summary",
            project_path=project_path,
            timeout=timeout_seconds,
            layer=layer,
        )

    @server.tool(name="get_layers_tree", description=_LAYERS_TREE_DESCRIPTION)
    def get_layers_tree(
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Return the layer panel structure.

        Args:
            project_path: Project to read. Defaults to the configured project.
            timeout_seconds: Override the configured timeout.
        """
        return run(
            "get_layers_tree",
            "layers_tree",
            project_path=project_path,
            timeout=timeout_seconds,
        )

    @server.tool(name="get_missing_layers", description=_MISSING_LAYERS_DESCRIPTION)
    def get_missing_layers(
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """List layers with broken data sources.

        Args:
            project_path: Project to read. Defaults to the configured project.
            timeout_seconds: Override the configured timeout.
        """
        return run(
            "get_missing_layers",
            "missing_layers",
            project_path=project_path,
            timeout=timeout_seconds,
        )

    @server.tool(
        name="get_project_path_info", description=_PROJECT_PATH_INFO_DESCRIPTION
    )
    def get_project_path_info(
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Report project file path and save state.

        Args:
            project_path: Project to read. Defaults to the configured project.
            timeout_seconds: Override the configured timeout.
        """
        return run(
            "get_project_path_info",
            "project_path_info",
            project_path=project_path,
            timeout=timeout_seconds,
        )

    return None


__all__ = ["register"]
