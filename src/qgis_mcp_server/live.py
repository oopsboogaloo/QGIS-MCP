"""The server's side of the live socket (task 6, REQ-1).

REQ-1: when a live QGIS session with the listener plugin active is available,
the server connects to it over a local TCP socket. This is that connection.

Two responsibilities, and they pull in opposite directions:

* **Answer "is a live session reachable?" cheaply.** Every mode-agnostic tool
  asks, via ``modes.resolve``, on every call. A socket connect per question
  would add latency to tools that never touch the live path at all, so the
  answer is cached for a few seconds.
* **Never claim liveness that is not there.** REQ-1a's whole point is that a
  live-mode call which silently ran headless is indistinguishable from a
  failure. A stale "yes" is worse than a slow "no", so the cache is short and
  a failed call invalidates it immediately.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, ExecutionMode, error
from qgis_mcp_protocol.socket_frame import (
    MAX_RESPONSE_FRAME_BYTES,
    BIND_HOST,
    DEFAULT_PORT,
    FrameError,
    LineReader,
    Request,
    decode_response,
    encode_request,
)

#: How long a reachability answer is reused.
#:
#: Short on purpose. The plugin can be toggled from the Plugins menu at any
#: moment, and a tool acting on a stale "reachable" would send a command into
#: a closed socket. A few seconds keeps the common case -- several tool calls
#: in one turn -- down to a single probe without the answer going stale across
#: a user's interaction with QGIS.
REACHABILITY_TTL = 5.0

#: Connect timeout for the probe. Loopback either answers at once or is not
#: listening, so a long timeout only delays the "no".
PROBE_TIMEOUT = 1.5

#: Default ceiling on one live command. The main thread may legitimately be
#: busy -- ``design.md`` §3.1 accepts that a long operation freezes the GUI --
#: so this matches the plugin's own job timeout rather than undercutting it.
DEFAULT_CALL_TIMEOUT = 300.0


@dataclass
class LiveSession:
    """What a successful probe learned about the session on the other end."""

    port: int
    qgis_version: str = ""
    project_file: str | None = None
    layer_count: int = 0
    has_unsaved_changes: bool = False
    interface_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "qgis_version": self.qgis_version,
            "project_file": self.project_file,
            "layer_count": self.layer_count,
            "has_unsaved_changes": self.has_unsaved_changes,
            "interface_available": self.interface_available,
        }


@dataclass
class LiveClient:
    """Talks to the listener plugin inside a running QGIS."""

    host: str = BIND_HOST
    port: int = DEFAULT_PORT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    #: Set to refuse the live path outright, for a user who wants the server
    #: headless-only even with QGIS open.
    enabled: bool = True

    _lock: Any = field(default_factory=threading.Lock, repr=False)
    _session: LiveSession | None = field(default=None, repr=False)
    _checked_at: float = field(default=0.0, repr=False)
    _last_error: str = field(default="", repr=False)

    # -- reachability ------------------------------------------------------

    def is_reachable(self, *, force: bool = False) -> bool:
        """Whether a live session is answering, cached for a few seconds."""
        return self.session(force=force) is not None

    def session(self, *, force: bool = False) -> LiveSession | None:
        """The live session's details, or ``None`` if none is reachable."""
        if not self.enabled:
            self._last_error = "the live path is disabled in configuration"
            return None

        with self._lock:
            fresh = (time.monotonic() - self._checked_at) < REACHABILITY_TTL
            if fresh and not force:
                return self._session

            envelope = self._call_once("ping", {}, timeout=PROBE_TIMEOUT)
            self._checked_at = time.monotonic()
            if not envelope.ok:
                self._session = None
                self._last_error = (envelope.error or {}).get("message", "unreachable")
                return None

            result = envelope.result or {}
            self._session = LiveSession(
                port=self.port,
                qgis_version=str(result.get("qgis_version") or ""),
                project_file=result.get("project_file"),
                layer_count=int(result.get("layer_count") or 0),
                has_unsaved_changes=bool(result.get("has_unsaved_changes")),
                interface_available=bool(result.get("interface_available")),
            )
            self._last_error = ""
            return self._session

    @property
    def last_error(self) -> str:
        """Why the last probe failed, for a mode-unavailable message."""
        return self._last_error or "no live QGIS session is listening"

    def invalidate(self) -> None:
        """Forget the cached answer.

        Called after any failed command: whatever the cache said a moment ago,
        a command that could not be delivered is evidence the session has gone.
        """
        with self._lock:
            self._checked_at = 0.0
            self._session = None

    # -- calling -----------------------------------------------------------

    def call(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Envelope:
        """Send one command and return its envelope.

        Never raises for a transport problem: an unreachable session comes back
        as a ``MODE_UNAVAILABLE`` envelope naming the live mode, which is what
        REQ-1a requires of a tool that names its mode.
        """
        if not self.enabled:
            return self._unavailable("the live path is disabled in configuration")

        envelope = self._call_once(
            command, params or {}, timeout=timeout or self.call_timeout
        )
        if not envelope.ok and (envelope.error or {}).get(
            "code"
        ) == ErrorCode.MODE_UNAVAILABLE:
            self.invalidate()
        return envelope

    def _call_once(
        self, command: str, params: dict[str, Any], *, timeout: float
    ) -> Envelope:
        """One connect-send-receive-close cycle.

        A fresh connection per command rather than a kept-open one. The
        listener serves a single client at a time, so holding the connection
        would mean this server owns the only slot for its whole lifetime --
        locking out a second agent, and leaving the slot stuck if this process
        died without closing. A loopback connect is cheap enough that the
        trade is not close.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(encode_request(Request(command=command, params=params)))
                # The response limit, not the request one: a screenshot is
                # legitimately larger than anything a client ever sends.
                reader = LineReader(sock, max_frame=MAX_RESPONSE_FRAME_BYTES)
                line = reader.read_line()
        except (OSError, socket.timeout) as exc:
            return self._unavailable(f"could not reach the live session: {exc}")
        except FrameError as exc:
            return self._unavailable(f"the live session sent a bad frame: {exc}")

        if line is None:
            return self._unavailable(
                "the live session closed the connection without replying"
            )
        try:
            return decode_response(line)
        except FrameError as exc:
            return self._unavailable(f"could not read the live reply: {exc}")

    def _unavailable(self, detail: str) -> Envelope:
        return error(
            ErrorCode.MODE_UNAVAILABLE,
            f"live execution is unavailable: {detail}. Open QGIS and enable "
            f"the MCP listener from Plugins -> QGIS MCP -> Start MCP listener, "
            f"or use a headless tool instead.",
            mode=ExecutionMode.LIVE,
            missing_mode=ExecutionMode.LIVE,
            expected_port=self.port,
        )


__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "PROBE_TIMEOUT",
    "REACHABILITY_TTL",
    "LiveClient",
    "LiveSession",
]
