"""Shapes exchanged across the process boundary between the server and QGIS.

This package is the shared contract described in ``spec/design.md`` §2.3 and is
vendored into both sides. Two constraints govern everything in it:

1. **No PyQGIS imports, ever.** It must import cleanly in the server's ordinary
   virtualenv *and* under the QGIS-bundled interpreter. The server reaches QGIS
   by crossing a process boundary (§2.2) and puts this package on the child's
   ``PYTHONPATH`` so both halves speak one contract rather than two.

2. **No syntax newer than the Python QGIS bundles.** QGIS 4.2 bundles 3.12. The
   server may one day run on something newer; the plugin and the headless child
   will not. Target 3.12 here even when the server could run more.

Standard library only.
"""

from qgis_mcp_protocol.envelope import (
    PROTOCOL_VERSION,
    ENVELOPE_BEGIN,
    ENVELOPE_END,
    ErrorCode,
    ExecutionMode,
    Envelope,
    EnvelopeError,
    ok,
    error,
    encode,
    decode,
    extract,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ENVELOPE_BEGIN",
    "ENVELOPE_END",
    "ErrorCode",
    "ExecutionMode",
    "Envelope",
    "EnvelopeError",
    "ok",
    "error",
    "encode",
    "decode",
    "extract",
]
