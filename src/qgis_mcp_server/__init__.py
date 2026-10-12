"""The MCP server half: MCP plumbing, the two execution paths, the tool surface.

Nothing in this package imports PyQGIS (``spec/design.md`` section 2.2). It
reaches QGIS only across a process boundary -- a child process on the headless
path, a TCP socket on the live one -- which is what lets the server run in an
ordinary virtualenv and keeps the PyQGIS path problem confined to constructing
an environment for someone else.
"""

__version__ = "0.1.0"
