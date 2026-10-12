"""The result envelope carried across the server/QGIS process boundary.

REQ-3 fixes the calling convention for both raw-execution tools: agent-authored
code returns data by assigning it to a ``result`` variable, and only
JSON-serialisable data comes back. This module is where that convention is
encoded, so the server and the QGIS-side runner cannot drift.

**Why the envelope is sentinel-delimited.** A QGIS child process does not have
clean stdout. Provider plugins, GDAL, PROJ and Qt all write warnings there, and
what they write varies by install and by the data touched. Scanning for a bare
JSON document would make the parse hostage to whatever else decided to print.
The runner therefore brackets its one JSON line between two sentinels that will
not occur in GIS chatter, and the server extracts from between them. Anything
outside the sentinels is diagnostic noise and is kept separately rather than
discarded -- when a run fails, that noise is usually the explanation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

ENVELOPE_BEGIN = "<<<QGIS_MCP_ENVELOPE>>>"
ENVELOPE_END = "<<<END_QGIS_MCP_ENVELOPE>>>"


class ExecutionMode:
    """The two execution paths of ``spec/design.md`` section 3.

    REQ-1b requires mode-agnostic tools to report which path served a call --
    "the layer was added" means two different things depending on the answer.
    REQ-1a requires mode-naming tools to refuse the other path rather than
    silently substituting it, so these values also name what went missing.
    """

    LIVE = "live"
    HEADLESS = "headless"


class ErrorCode:
    """Stable, machine-checkable failure kinds.

    Separate from the human-readable message so that tests and callers can
    branch on the kind of failure without matching on prose.
    """

    #: The named execution mode is not available (REQ-1a). Carries the mode.
    MODE_UNAVAILABLE = "mode_unavailable"
    #: The configured QGIS install is missing, incomplete or unidentifiable.
    #: Raised at startup by preference, not at first call (Task 1 acceptance).
    QGIS_NOT_FOUND = "qgis_not_found"
    #: Agent-authored code raised. Carries the child-side traceback.
    CODE_RAISED = "code_raised"
    #: Code ran, but what it left in ``result`` will not serialise (REQ-3).
    RESULT_NOT_SERIALISABLE = "result_not_serialisable"
    #: The child process exceeded its timeout and was terminated.
    TIMEOUT = "timeout"
    #: The child produced no envelope, or an unparseable one -- it usually died
    #: before the runner could emit anything.
    NO_ENVELOPE = "no_envelope"
    #: The child exited non-zero without emitting a structured failure.
    CHILD_FAILED = "child_failed"
    #: A required input was missing or malformed.
    BAD_REQUEST = "bad_request"


class EnvelopeError(Exception):
    """Raised when a payload cannot be read as an envelope."""


@dataclass
class Envelope:
    """One structured result crossing the boundary.

    ``ok`` discriminates: a successful envelope carries ``result``, a failed one
    carries ``error``. ``diagnostics`` holds whatever the child wrote outside
    the sentinels, which is where the real explanation usually lives.
    """

    ok: bool
    protocol: int = PROTOCOL_VERSION
    result: Any = None
    error: dict[str, Any] | None = None
    mode: str | None = None
    diagnostics: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise, omitting empty optional fields to keep payloads readable."""
        out: dict[str, Any] = {"protocol": self.protocol, "ok": self.ok}
        if self.ok:
            out["result"] = self.result
        else:
            out["error"] = self.error or {}
        if self.mode is not None:
            out["mode"] = self.mode
        if self.diagnostics:
            out["diagnostics"] = self.diagnostics
        if self.meta:
            out["meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Envelope:
        if not isinstance(payload, dict):
            raise EnvelopeError(
                f"envelope must be a JSON object, got {type(payload).__name__}"
            )
        if "ok" not in payload:
            raise EnvelopeError(
                f"envelope has no ok discriminator; keys present: {sorted(payload)}"
            )
        version = payload.get("protocol", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise EnvelopeError(
                f"envelope protocol version {version!r} is not supported "
                f"(this build speaks {PROTOCOL_VERSION})"
            )
        return cls(
            ok=bool(payload["ok"]),
            protocol=version,
            result=payload.get("result"),
            error=payload.get("error"),
            mode=payload.get("mode"),
            diagnostics=payload.get("diagnostics"),
            meta=payload.get("meta") or {},
        )


def ok(result: Any, *, mode: str | None = None, **meta: Any) -> Envelope:
    """Build a successful envelope."""
    return Envelope(ok=True, result=result, mode=mode, meta=dict(meta))


def error(
    code: str,
    message: str,
    *,
    mode: str | None = None,
    **detail: Any,
) -> Envelope:
    """Build a failed envelope.

    ``detail`` is merged into the error object rather than kept beside it, so a
    caller reading ``envelope.error`` sees the code, the message and every
    specific in one place.
    """
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update({k: v for k, v in detail.items() if v is not None})
    return Envelope(ok=False, error=payload, mode=mode)


def encode(envelope: Envelope) -> str:
    """Render an envelope as the sentinel-delimited block the runner emits.

    One line of JSON between two sentinel lines. Single-line so that a child
    interleaving its own writes cannot split the payload.
    """
    body = json.dumps(envelope.to_dict(), default=str, separators=(",", ":"))
    return ENVELOPE_BEGIN + "\n" + body + "\n" + ENVELOPE_END


def decode(body: str) -> Envelope:
    """Parse a bare JSON envelope body."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"envelope is not valid JSON: {exc}") from exc
    return Envelope.from_dict(payload)


def extract(stream: str) -> tuple[Envelope, str]:
    """Pull the envelope out of a child's stdout.

    Returns the envelope and the surrounding output, the latter also attached to
    the envelope as ``diagnostics``.

    **Matching is line-anchored, and that is the whole subtlety.** A substring
    search for the sentinel finds the wrong one. The runner captures
    agent-authored code's stdout and puts it *inside* the envelope, so code that
    prints something sentinel-shaped ends up with that text nested in the
    envelope's own JSON -- and JSON does not escape ``<`` or ``>``, so it sits
    there as literal, findable text. A search from either end can therefore land
    on a copy embedded in the payload it was trying to read.

    What separates the real pair from an embedded one is position, not content:
    the runner writes each sentinel alone on its own line, while an embedded
    copy is always mid-line, because the newlines around it were escaped as
    ``\\n`` when the captured output was serialised into the JSON. So entire
    lines are compared, and the last such pair wins.

    Raises ``EnvelopeError`` when there is no envelope to find -- which is a
    diagnosis in itself, since it means the child died before the runner could
    report anything.
    """
    lines = stream.splitlines()
    begin_index = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == ENVELOPE_BEGIN:
            begin_index = index
            break
    if begin_index is None:
        raise EnvelopeError(
            "no envelope in child output -- the process probably died before "
            "the runner could emit one"
        )

    end_index = None
    for index in range(begin_index + 1, len(lines)):
        if lines[index].strip() == ENVELOPE_END:
            end_index = index
            break
    if end_index is None:
        raise EnvelopeError(
            "envelope opened but never closed -- the child was most likely "
            "killed part-way through writing it"
        )

    envelope = decode("\n".join(lines[begin_index + 1:end_index]).strip())
    noise = "\n".join(lines[:begin_index] + lines[end_index + 1:]).strip()
    envelope.diagnostics = noise or None
    return envelope, noise
