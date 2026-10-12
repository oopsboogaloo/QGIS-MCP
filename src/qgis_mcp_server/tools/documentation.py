"""PyQGIS documentation tools (task 7).

``search_pyqgis_docs`` and ``get_pyqgis_class_docs``, satisfying REQ-6.

Both are mode-agnostic under REQ-1b, and search is the only tool in this server
that touches neither QGIS nor the network: the index comes from the type stubs
on disk, parsed in this process.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_server import modes
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.pyqgis_docs import DEFAULT_RESULT_LIMIT
from qgis_mcp_server.tools.result import tool_result

_SEARCH_DESCRIPTION = """\
Look up a PyQGIS class or method in the API of the QGIS build actually installed.

Accepts a class (`QgsVectorLayer`), a member (`QgsVectorLayer.featureCount`), a
fully qualified name (`qgis.core.QgsVectorLayer`), or free text (`zonal`).

It never just says "not found". The `match` field says which kind of answer you
got:

  exact_class      / exact_member    the thing exists; here it is
  namespace_only   the class exists, the member does not — the closest member
                   names and its siblings come back, so a near-miss costs one
                   round-trip instead of a dead end
  nearest_classes  no such class; the closest names
  text             nothing is named that; these names contain it

Signatures include every overload, which matters: PyQGIS methods are frequently
overloaded and picking the wrong one is a TypeError at runtime.

The index is pinned to the installed QGIS version. A class added in a later
release will not be here, and that is the honest answer rather than a guess —
PyQGIS tracks the C++ API and both move every release.
"""

_CLASS_DOCS_DESCRIPTION = """\
Full documentation for one PyQGIS class: every member's signature, plus prose.

Signatures come from the installed build's type stubs, so they describe exactly
what you can call. The explanatory text is read from the running bindings, which
costs a couple of seconds the first time a class is asked about and nothing
afterwards.

Pass `members` for the full write-up of specific methods — return values,
exceptions, the `:param:` notes. Without it you get the class description plus a
one-line summary of every member, which is usually what you want for finding
your way around a class.

If prose cannot be read — a few SIP-internal names are in the stubs but are not
importable — the signatures still come back, with `prose_unavailable` saying
why. A partial answer beats none.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the documentation tools on ``server``."""

    @server.tool(name="search_pyqgis_docs", description=_SEARCH_DESCRIPTION)
    def search_pyqgis_docs(
        query: str,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> dict[str, Any]:
        """Search the PyQGIS API (REQ-6).

        Args:
            query: A class name, a ``Class.member``, or free text.
            limit: Maximum suggestions to return.
        """
        decision = modes.resolve("search_pyqgis_docs")
        return tool_result(context.docs.search(query, limit=limit), decision)

    @server.tool(
        name="get_pyqgis_class_docs", description=_CLASS_DOCS_DESCRIPTION
    )
    def get_pyqgis_class_docs(
        class_name: str,
        members: list[str] | None = None,
        include_summaries: bool = True,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Return a class's full API and documentation.

        Args:
            class_name: Class to document, e.g. ``QgsVectorLayer``. Nested
                classes work too: ``QgsVectorLayer.LayerOptions``.
            members: Members to document in full. Omit for one-line summaries
                of all of them.
            include_summaries: Include a one-line summary per member.
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("get_pyqgis_class_docs")
        return tool_result(
            context.docs.class_docs(
                class_name,
                members=members,
                include_summaries=include_summaries,
                timeout=timeout_seconds,
            ),
            decision,
        )

    return None


__all__ = ["register"]
