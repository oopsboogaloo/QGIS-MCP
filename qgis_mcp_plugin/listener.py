"""The TCP listener (task 6.2).

Accepts on a background thread, reads on a background thread, and executes
nothing there. Every command is handed to :class:`MainThreadMarshaller`, which
runs it on the QGIS main thread and hands back the result -- see
``marshaller.py`` for why that boundary is shaped the way it is.

**One connection at a time, on purpose.** The work all serialises on the main
thread anyway, so concurrency would buy nothing but a longer list of ways to
interleave state. A second client is accepted and told plainly that another is
connected rather than being left to wonder.

**Bound to loopback only.** This socket executes arbitrary Python inside the
user's QGIS session. The bind address is a constant in the shared protocol
module rather than a setting, because there is no configuration of it that is
a good idea.
"""

from __future__ import annotations

import socket
import threading
import traceback
from typing import Any, Callable

from qgis_mcp_protocol import ErrorCode, ExecutionMode, error, ok
from qgis_mcp_protocol.socket_frame import (
    BIND_HOST,
    DEFAULT_PORT,
    FrameError,
    LineReader,
    decode_request,
    encode_response,
)

from .errors import CommandError
from .marshaller import MainThreadMarshaller, MainThreadTimeout

#: How long a connected client may sit idle before the socket is recycled.
#:
#: Long enough that a user thinking between commands is not disconnected,
#: short enough that a crashed client does not hold the only connection slot
#: until QGIS restarts.
CLIENT_IDLE_TIMEOUT = 600.0

#: How long to wait for the accept loop to notice a stop request.
SHUTDOWN_GRACE = 2.0


class QgisMcpListener:
    """A persistent background listener, not an interactive REPL."""

    def __init__(
        self,
        marshaller: MainThreadMarshaller,
        handlers: dict[str, Callable[[dict[str, Any]], Any]],
        *,
        host: str = BIND_HOST,
        port: int = DEFAULT_PORT,
        job_timeout: float = 300.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._marshaller = marshaller
        self._handlers = dict(handlers)
        self._host = host
        self._port = port
        self._job_timeout = job_timeout
        self._log = log or (lambda message: None)

        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        self._client_connected = False
        self.bound_port: int | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> int:
        """Bind, listen, and return the port actually bound.

        Raises ``OSError`` if the port is taken -- reported to the user rather
        than silently trying another, since an agent configured for one port
        would not find the listener on a different one.
        """
        if self.running:
            raise RuntimeError("the listener is already running")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self._host, self._port))
        except OSError:
            server.close()
            raise
        server.listen(1)
        # A timeout on the listening socket is what lets the accept loop
        # notice a stop request. Without it, accept() blocks forever and
        # unloading the plugin would leave a thread alive until QGIS exits.
        server.settimeout(0.5)

        self._server = server
        self.bound_port = server.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="qgis-mcp-listener",
            # Daemon, so a listener that somehow fails to stop cannot keep
            # QGIS from exiting. Losing the thread at shutdown is harmless;
            # blocking the user's quit is not.
            daemon=True,
        )
        self._thread.start()
        self._log(f"listening on {self._host}:{self.bound_port}")
        return self.bound_port

    def stop(self) -> None:
        """Stop accepting and close the socket."""
        self._stop.set()
        thread, self._thread = self._thread, None
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(SHUTDOWN_GRACE)
        self.bound_port = None
        self._log("listener stopped")

    # -- background thread -------------------------------------------------

    def _accept_loop(self) -> None:
        """Accept connections until stopped. **Background thread.**"""
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                # The socket was closed under us, which is how stop() works.
                return

            with self._client_lock:
                already = self._client_connected
                if not already:
                    self._client_connected = True

            if already:
                # Refused with an explanation rather than dropped: a silently
                # closed connection looks like the listener is broken.
                self._refuse(client, "another client is already connected")
                continue

            # Served on its own thread so the accept loop stays free.
            #
            # Calling _serve inline here was a bug: it blocks for as long as
            # the client stays connected, which is the whole session -- so
            # accept() was never reached again and a second client sat in the
            # kernel's backlog, connected at the TCP level and never answered.
            # The refusal above was unreachable, and the symptom was a client
            # that hung rather than one that was told why.
            worker = threading.Thread(
                target=self._serve_guarded,
                args=(client, address),
                name="qgis-mcp-connection",
                daemon=True,
            )
            worker.start()

    def _serve_guarded(self, client: socket.socket, address) -> None:
        """Serve one connection, releasing the slot however it ends."""
        try:
            self._serve(client, address)
        except Exception:  # noqa: BLE001 - a worker must never die silently
            self._log("connection handler failed:\n" + traceback.format_exc())
        finally:
            with self._client_lock:
                self._client_connected = False

    def _refuse(self, client: socket.socket, message: str) -> None:
        try:
            client.sendall(
                encode_response(
                    error(
                        ErrorCode.BAD_REQUEST, message, mode=ExecutionMode.LIVE
                    )
                )
            )
        except OSError:
            pass
        finally:
            _close(client)

    def _serve(self, client: socket.socket, address) -> None:
        """Read requests and write responses. **Background thread.**"""
        self._log(f"client connected from {address[0]}:{address[1]}")
        client.settimeout(CLIENT_IDLE_TIMEOUT)
        reader = LineReader(client)
        try:
            while not self._stop.is_set():
                try:
                    line = reader.read_line()
                except socket.timeout:
                    self._log("client idle, closing")
                    return
                except FrameError as exc:
                    self._send(
                        client,
                        error(
                            ErrorCode.BAD_REQUEST,
                            f"malformed frame: {exc}",
                            mode=ExecutionMode.LIVE,
                        ),
                    )
                    return
                if line is None:
                    self._log("client disconnected")
                    return
                if not line.strip():
                    continue
                self._send(client, self._handle(line))
        finally:
            _close(client)

    def _handle(self, line: bytes):
        """Turn one request frame into a response envelope.

        Every outcome is an envelope. Nothing raised in here is allowed to
        reach the accept loop, because an exception that killed the connection
        would leave the caller with a closed socket and no explanation.
        """
        try:
            request = decode_request(line)
        except FrameError as exc:
            return error(
                ErrorCode.BAD_REQUEST, str(exc), mode=ExecutionMode.LIVE
            )

        handler = self._handlers.get(request.command)
        if handler is None:
            return error(
                ErrorCode.BAD_REQUEST,
                f"unknown command {request.command!r}",
                mode=ExecutionMode.LIVE,
                command=request.command,
                # Candidates rather than a bare refusal, the same shape the
                # rest of this server uses (requirements.md §0).
                supported_commands=sorted(self._handlers),
            )

        try:
            # The one line that crosses the thread boundary. Everything the
            # handler does happens on the main thread.
            result = self._marshaller.submit(
                lambda: handler(request.params), timeout=self._job_timeout
            )
        except MainThreadTimeout as exc:
            return error(
                ErrorCode.TIMEOUT,
                str(exc),
                mode=ExecutionMode.LIVE,
                command=request.command,
            )
        except CommandError as exc:
            # A command's own refusal, with its structured detail intact.
            # Stringifying it would discard the part the caller needs -- most
            # sharply for CodeRaised, which carries the REQ-4a backup path.
            return error(
                exc.code,
                str(exc),
                mode=ExecutionMode.LIVE,
                command=request.command,
                **exc.detail,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return error(
                ErrorCode.CODE_RAISED,
                f"{type(exc).__name__}: {exc}",
                mode=ExecutionMode.LIVE,
                command=request.command,
                exception_type=type(exc).__name__,
                traceback=traceback.format_exc(),
            )

        if isinstance(result, dict):
            return ok(result, mode=ExecutionMode.LIVE, command=request.command)
        return ok(
            {"result": result}, mode=ExecutionMode.LIVE, command=request.command
        )

    def _send(self, client: socket.socket, envelope) -> None:
        try:
            client.sendall(encode_response(envelope))
        except OSError as exc:
            self._log(f"could not write a response: {exc}")


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


__all__ = ["CLIENT_IDLE_TIMEOUT", "QgisMcpListener"]
