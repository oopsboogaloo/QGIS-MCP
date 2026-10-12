"""The QGIS MCP listener plugin (task 6).

Exposes a running QGIS session to the qgis-mcp server over a loopback TCP
socket. See ``marshaller.py`` for the threading boundary, which is the part of
this plugin that matters most: PyQGIS GUI objects are main-thread-only, and
touching them from the socket thread segfaults QGIS rather than raising.
"""


from ._bootstrap import ensure_protocol_importable

#: Where the shared protocol package was found, for the plugin's load log.
#:
#: Run at import rather than inside ``classFactory`` so the failure arrives
#: while QGIS is still reporting plugin load errors, and names the directories
#: it searched instead of surfacing as a bare ImportError from a submodule.
PROTOCOL_SOURCE = ensure_protocol_importable()


def classFactory(iface):  # noqa: N802 - the name QGIS looks for
    """Entry point QGIS calls to instantiate the plugin.

    Imported lazily so that an import error in the plugin body surfaces as a
    plugin load failure QGIS can report, rather than breaking the scan of the
    plugin directory.
    """
    from .plugin import QgisMcpPlugin

    return QgisMcpPlugin(iface)
