"""Framing for the live listener's TCP socket (``spec/design.md`` §3.1).

The headless path hands an envelope back on stdout, bracketed by sentinels
because QGIS pollutes that stream. A socket has no such problem: nothing else
writes to it. So the framing here is simpler and stricter -- one JSON document
per line, newline-terminated, in both directions.

**Why newline-delimited rather than length-prefixed.** Both work. Newlines win
on one practical point: a socket conversation that goes wrong can be read with
``nc`` or a two-line Python script, which matters for a component whose failure
mode is a QGIS crash. A length prefix would make the same inspection a
hex-dumping exercise at exactly the moment nobody wants one.

The envelope JSON is always emitted on a single line (``separators`` leaves no
newlines, and any newline inside a string is escaped by ``json``), so a line is
always a whole message.

This module is part of the contract vendored into both sides (§2.3): standard
library only, no PyQGIS, and no syntax newer than the Python QGIS bundles.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from qgis_mcp_protocol.envelope import (
    PROTOCOL_VERSION,
    Envelope,
    EnvelopeError,
)

#: One message per line, in both directions.
FRAME_TERMINATOR = b"\n"

#: Refuse a line longer than this rather than buffering without limit.
#:
#: A request is a command name and a parameter dict; agent-authored code is the
#: largest thing one can carry and that is still kilobytes. A megabyte means
#: either a bug or something hostile, and a listener bound to a local port
#: should not be the thing that exhausts QGIS's memory.
MAX_FRAME_BYTES = 1_048_576

#: Default port for the listener. Registered nowhere -- high, unprivileged, and
#: unlikely to collide with a developer's usual services.
DEFAULT_PORT = 9876

#: Loopback only, and deliberately not configurable to anything else.
#:
#: This socket executes arbitrary Python inside the user's QGIS session. Bound
#: to 0.0.0.0 it would be a remote code execution hole on any network the
#: machine is attached to, and no amount of later authentication would make
#: that a sensible default.
BIND_HOST = "127.0.0.1"


class FrameError(Exception):
    """A frame could not be read or parsed."""


@dataclass
class Request:
    """One command for the live listener."""

    command: str
    params: dict[str, Any] = field(default_factory=dict)
    #: Echoed back on the response, so a caller can match them up. Not used
    #: for multiplexing: the listener answers one request at a time.
    request_id: str = ""
    protocol: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "command": self.command,
        }
        if self.params:
            payload["params"] = self.params
        if self.request_id:
            payload["id"] = self.request_id
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Request:
        if not isinstance(payload, dict):
            raise FrameError(
                f"a request must be a JSON object, got {type(payload).__name__}"
            )
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            raise FrameError(
                f"a request needs a 'command' string; keys present: "
                f"{sorted(payload)}"
            )
        version = payload.get("protocol", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise FrameError(
                f"request protocol version {version!r} is not supported "
                f"(this build speaks {PROTOCOL_VERSION}). The plugin and the "
                f"server are from different releases."
            )
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise FrameError(
                f"'params' must be a JSON object, got {type(params).__name__}"
            )
        return cls(
            command=command.strip(),
            params=params,
            request_id=str(payload.get("id") or ""),
            protocol=version,
        )


def encode_request(request: Request) -> bytes:
    """Render a request as one newline-terminated JSON line."""
    body = json.dumps(request.to_dict(), separators=(",", ":"), default=str)
    return body.encode("utf-8") + FRAME_TERMINATOR


def decode_request(line: bytes | str) -> Request:
    """Parse one request frame."""
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        raise FrameError("empty request frame")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameError(f"request is not valid JSON: {exc}") from exc
    return Request.from_dict(payload)


def encode_response(envelope: Envelope) -> bytes:
    """Render an envelope as one newline-terminated JSON line."""
    body = json.dumps(envelope.to_dict(), separators=(",", ":"), default=str)
    return body.encode("utf-8") + FRAME_TERMINATOR


def decode_response(line: bytes | str) -> Envelope:
    """Parse one response frame into an envelope."""
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        raise FrameError("empty response frame")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameError(f"response is not valid JSON: {exc}") from exc
    try:
        return Envelope.from_dict(payload)
    except EnvelopeError as exc:
        raise FrameError(str(exc)) from exc


class LineReader:
    """Reads newline-delimited frames from a socket.

    A small buffered reader rather than ``socket.makefile``, for one reason
    that matters here: it enforces :data:`MAX_FRAME_BYTES` while reading rather
    than after, so a sender that never sends a newline cannot make the listener
    accumulate until QGIS runs out of memory.
    """

    def __init__(self, sock, *, max_frame: int = MAX_FRAME_BYTES) -> None:
        self._socket = sock
        self._buffer = b""
        self._max_frame = max_frame

    def read_line(self) -> bytes | None:
        """The next frame without its terminator, or ``None`` at end of stream.

        Raises ``FrameError`` when the peer sends more than ``max_frame`` bytes
        without a terminator, and ``socket.timeout`` when the socket's own
        timeout elapses -- which the caller wants to distinguish, since one is
        a protocol violation and the other is an idle client.
        """
        while True:
            index = self._buffer.find(FRAME_TERMINATOR)
            if index >= 0:
                line, self._buffer = self._buffer[:index], self._buffer[index + 1 :]
                return line
            if len(self._buffer) > self._max_frame:
                raise FrameError(
                    f"frame exceeded {self._max_frame} bytes with no newline; "
                    f"refusing to buffer more"
                )
            chunk = self._socket.recv(8192)
            if not chunk:
                # Clean end of stream. A partial frame here is the peer having
                # gone away mid-message, which is reported rather than parsed.
                if self._buffer.strip():
                    raise FrameError(
                        "the connection closed part-way through a frame"
                    )
                return None
            self._buffer += chunk


__all__ = [
    "BIND_HOST",
    "DEFAULT_PORT",
    "FRAME_TERMINATOR",
    "MAX_FRAME_BYTES",
    "FrameError",
    "LineReader",
    "Request",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]
