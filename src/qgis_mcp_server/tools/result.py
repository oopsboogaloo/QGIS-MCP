"""The shape every tool returns.

One function, so that the contract is uniform across the tool surface rather
than re-decided per module. Three things are load-bearing in it:

**Failures are returned, not raised.** Every contract this server is built on
asks an error to carry structured detail the caller can act on -- REQ-1a's named
missing mode, REQ-6/7/9/12's candidate lists, REQ-10's per-provider statuses.
An exception reduces all of that to a string, and the caller's next move usually
depends on the detail.

**``mode`` is always present.** REQ-1b makes it load-bearing for mode-agnostic
tools: for anything that registers a layer it is the only way to know whether
the result landed in the open canvas or in a project file on disk. Keeping the
key uniform means a caller never has to remember which tools report it.

**``mode_reason`` comes with it.** "Headless" alone does not answer the question
a caller actually has, which is why their layer is not on screen.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_protocol import Envelope
from qgis_mcp_server.modes import ModeDecision


def tool_result(envelope: Envelope, decision: ModeDecision) -> dict[str, Any]:
    """Flatten an envelope into a tool's return value.

    A dict result is spread into the payload, since it is already a map of
    named facts and nesting it under ``result`` would add a level for nothing.
    Any other value is placed under ``result``, where it has a name.
    """
    payload: dict[str, Any] = {
        "ok": envelope.ok,
        # The executing mode wins when the envelope knows it; the dispatcher's
        # decision is the fallback for a call that never reached a child.
        "mode": envelope.mode or decision.mode,
        "mode_reason": decision.reason,
    }

    if envelope.ok:
        if isinstance(envelope.result, dict):
            payload.update(envelope.result)
        else:
            payload["result"] = envelope.result
        for key, value in envelope.meta.items():
            if value is not None:
                payload[key] = value
    else:
        payload["error"] = envelope.error or {}

    if envelope.diagnostics:
        payload["diagnostics"] = envelope.diagnostics
    return payload


__all__ = ["tool_result"]
