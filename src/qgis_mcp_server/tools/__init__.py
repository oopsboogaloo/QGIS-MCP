"""Tool registration.

Tools are registered per module rather than from one list, so that the mode
partition of ``design.md`` section 3.3 stays visible in the file layout: a
module knows which execution path it belongs to, and the dispatcher consults
the partition rather than inferring the mode from context.
"""

from qgis_mcp_server.tools import raw_execution

#: Registered in order. Each module exposes ``register(server, context)``.
MODULES = (raw_execution,)


def register_all(server, context) -> None:
    """Register every implemented tool on ``server``."""
    for module in MODULES:
        module.register(server, context)


__all__ = ["MODULES", "register_all"]
