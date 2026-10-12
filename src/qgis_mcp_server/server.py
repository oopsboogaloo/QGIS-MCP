"""Builds the MCP server.

``spec/design.md`` §2.4 settles the framework choice: the decorator API that
derives tool schemas from type hints via Pydantic, rather than hand-authored
JSON Schema. With roughly 28 tools carrying bounding boxes, type lists,
resolutions, formats and CRS identifiers, hand-written schemas would be both
the most tedious part of the build and the likeliest to drift.

**A naming note.** The spec calls this API *FastMCP*, which is what it was
called in the 1.x Python SDK. In SDK 2.x it is ``MCPServer``, the same decorator
API under a new name -- so the design decision stands and only the import
changed. Worth recording rather than silently diverging from the document.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_server.config import Config
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.tools import register_all

SERVER_NAME = "qgis-mcp"

INSTRUCTIONS = """\
Drives QGIS over two execution paths.

Headless tools run QGIS as a child process and need no GUI. Live tools require
a running QGIS with the listener plugin active. A tool that names its mode will
refuse rather than substitute the other one, so an error naming a missing mode
means exactly that -- not that the operation failed.

Tools that do not name a mode report which one served the call. Check it: for
anything that registers a layer, it is the difference between the layer landing
in the open canvas and landing in a project file on disk.
"""


def build_server(config: Config | None = None) -> tuple[Any, ServerContext]:
    """Construct the server and its context.

    Returns both: the caller owns the context's lifetime, since it holds the
    staged child payload that needs cleaning up on shutdown.
    """
    from mcp.server.mcpserver import MCPServer

    resolved = config or Config.load()
    context = ServerContext(resolved)
    server = MCPServer(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )
    register_all(server, context)
    return server, context


__all__ = ["SERVER_NAME", "build_server"]
