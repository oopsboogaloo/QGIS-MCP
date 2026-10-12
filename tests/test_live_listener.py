"""Task 6: the live listener, its threading boundary, and the live tools.

`tasks.md` 6.2 is blunt about the limits of testing here:

    A main-thread violation segfaults QGIS rather than raising -- there is no
    exception to catch, so this cannot be validated by a test that merely
    passes.

That holds for the *consequences* of a violation. The boundary itself turns out
to be testable, and that is worth a great deal: a headless
`QgsApplication([], False)` is still a `QApplication`, `exec()` runs a real
event loop, and a signal emitted from a worker thread is dispatched to the
thread that called `exec()` exactly as it would be in the GUI.

So `live_harness.py` runs the **real** marshaller, the **real** listener and
the **real** command handlers inside such an application, and the test below
proves across a socket that a command executed on the main thread -- by
comparing the thread id the handler saw against the one the harness reported
before it started listening.

What is still not covered, and is flagged rather than implied: the canvas and
window commands need a GUI. They report the interface as unavailable here, and
their behaviour against a real canvas is a one-off manual check.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import CollectingServer, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode
from qgis_mcp_protocol.socket_frame import (
    BIND_HOST,
    MAX_FRAME_BYTES,
    FrameError,
    LineReader,
    Request,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from qgis_mcp_server import modes
from qgis_mcp_server.live import LiveClient

HARNESS = Path(__file__).parent / "live_harness.py"

#: Long enough for a slow first QGIS start, short enough that a wedged harness
#: does not hold the suite open.
HARNESS_START_TIMEOUT = 120.0


class Harness:
    """A live listener running in a headless QGIS, for one test module."""

    def __init__(self, process, port: int, main_thread: int, info: dict) -> None:
        self.process = process
        self.port = port
        self.main_thread = main_thread
        self.info = info

    def connect(self) -> "Conversation":
        sock = socket.create_connection((BIND_HOST, self.port), timeout=60)
        sock.settimeout(60)
        return Conversation(sock)

    def stop(self) -> None:
        try:
            with self.connect() as conversation:
                conversation.call("shutdown")
        except OSError:
            pass
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


class Conversation:
    """One client connection, framed."""

    def __init__(self, sock: socket.socket) -> None:
        self.socket = sock
        self.reader = LineReader(sock)

    def __enter__(self) -> "Conversation":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def call(self, command: str, **params):
        self.socket.sendall(
            encode_request(Request(command=command, params=params))
        )
        line = self.reader.read_line()
        assert line is not None, f"the listener closed without replying to {command}"
        return decode_response(line)

    def read(self):
        line = self.reader.read_line()
        assert line is not None, "the listener closed without sending anything"
        return decode_response(line)

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


@pytest.fixture(scope="module")
def harness(config, fixture_project):
    """Start the listener in a headless QGIS, and stop it afterwards."""
    if not HARNESS.exists():
        pytest.skip("live_harness.py missing")

    process = subprocess.Popen(
        [
            str(config.install.python_exe),
            str(HARNESS),
            "--project",
            str(fixture_project),
            "--port",
            "0",
            "--lifetime",
            "300",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=config.install.child_env(),
        cwd=str(HARNESS.parent.parent),
    )

    deadline = time.monotonic() + HARNESS_START_TIMEOUT
    line = ""
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line.strip():
            break
        if process.poll() is not None:
            break
    if not line.strip():
        process.kill()
        pytest.skip("the live harness did not start")

    info = json.loads(line)
    if not info.get("ready"):
        process.kill()
        pytest.fail(f"the live harness failed to start: {describe(info)}")

    instance = Harness(process, info["port"], info["main_thread"], info)
    yield instance
    instance.stop()


# ---------------------------------------------------------------------------
# The threading boundary
# ---------------------------------------------------------------------------


def test_a_command_runs_on_the_qgis_main_thread(harness):
    """The assertion the whole of task 6 turns on.

    `ping` reports the thread it executed on. The harness reported the thread
    that called `exec()` before it began listening. If the handler had run on
    the socket thread -- the mistake that segfaults a real QGIS -- these would
    differ, and a request would still have appeared to succeed.
    """
    with harness.connect() as conversation:
        envelope = conversation.call("ping")

    assert envelope.ok, describe(envelope.error)
    executed_on = envelope.result["executed_on_thread"]
    assert executed_on == harness.main_thread, (
        f"the handler ran on thread {executed_on} but the QGIS main thread is "
        f"{harness.main_thread}. Work is not being marshalled, which in a real "
        f"session is a segfault rather than this assertion."
    )


def test_every_command_is_marshalled_not_just_ping(harness):
    """One handler being on the main thread does not prove the route is.

    `ping` is the command most likely to be special-cased. Running a second,
    unrelated command through the same path and checking the thread again is
    what makes it a property of the boundary rather than of one handler.
    """
    with harness.connect() as conversation:
        probe = conversation.call(
            "execute_pyqgis_code",
            code="import threading\nresult = threading.get_ident()",
        )
    assert probe.ok, describe(probe.error)
    assert probe.result["result"] == harness.main_thread, (
        f"agent code ran on thread {probe.result['result']}, not the main "
        f"thread {harness.main_thread}"
    )


def test_the_socket_thread_is_a_different_thread(harness):
    """The negative control, without which the two tests above are hollow.

    If the listener happened to serve on the main thread, every assertion
    about marshalling would hold for the wrong reason -- the work would be on
    the main thread because it never left it. So this establishes two separate
    facts from inside one handler: that the handler is on the interpreter's
    main thread, and that the connection thread serving the request is a
    distinct, live thread with a different identity.
    """
    with harness.connect() as conversation:
        envelope = conversation.call(
            "execute_pyqgis_code",
            code=(
                "import threading\n"
                "here = threading.current_thread()\n"
                "others = [\n"
                "    {'name': t.name, 'ident': t.ident, 'alive': t.is_alive()}\n"
                "    for t in threading.enumerate()\n"
                "    if t.ident != here.ident\n"
                "]\n"
                "result = {\n"
                "    'on_main_thread': here is threading.main_thread(),\n"
                "    'ident': here.ident,\n"
                "    'others': others,\n"
                "}"
            ),
        )
    assert envelope.ok, describe(envelope.error)
    report = envelope.result["result"]

    # Independent of the harness's reported id: Python's main_thread() is the
    # thread that started the interpreter, which is the one calling exec().
    assert report["on_main_thread"] is True, (
        f"the handler did not run on the interpreter's main thread: {describe(report)}"
    )

    serving = [
        thread
        for thread in report["others"]
        if "qgis-mcp-connection" in thread["name"]
    ]
    assert serving, (
        "no connection thread was alive while the handler ran, so the request "
        f"may never have crossed a thread boundary at all: {describe(report)}"
    )
    assert all(thread["ident"] != report["ident"] for thread in serving), (
        f"the connection thread and the handler share an identity: {describe(report)}"
    )


def test_mode_is_reported_as_live(harness):
    """REQ-1b: the response says which path served it."""
    with harness.connect() as conversation:
        envelope = conversation.call("ping")
    assert envelope.mode == ExecutionMode.LIVE, envelope.mode


# ---------------------------------------------------------------------------
# Protocol and framing
# ---------------------------------------------------------------------------


def test_request_round_trips():
    request = Request(command="ping", params={"a": 1}, request_id="x")
    decoded = decode_request(encode_request(request))
    assert decoded.command == "ping"
    assert decoded.params == {"a": 1}
    assert decoded.request_id == "x"


def test_frames_are_one_line_each():
    """The framing contract: a line is always a whole message."""
    frame = encode_request(Request(command="ping", params={"text": "a\nb"}))
    assert frame.endswith(b"\n")
    assert frame.count(b"\n") == 1, (
        "a newline inside a parameter must be escaped by json, not emitted "
        "raw, or it would split one message into two frames"
    )


@pytest.mark.parametrize(
    "payload, fragment",
    [
        (b"not json\n", "not valid JSON"),
        (b'{"params":{}}\n', "needs a 'command' string"),
        (b'{"command":"ping","protocol":99}\n', "not supported"),
        (b'{"command":"ping","params":[1,2]}\n', "must be a JSON object"),
        (b'[]\n', "must be a JSON object"),
    ],
)
def test_malformed_requests_are_refused_with_a_reason(payload, fragment):
    with pytest.raises(FrameError) as caught:
        decode_request(payload)
    assert fragment in str(caught.value), str(caught.value)


def test_protocol_mismatch_blames_the_version(harness):
    """A plugin and server from different releases should say so.

    The alternative is a confusing parse error at whichever end is newer.
    """
    with harness.connect() as conversation:
        conversation.socket.sendall(b'{"command":"ping","protocol":99}\n')
        envelope = conversation.read()
    assert not envelope.ok
    assert "different releases" in envelope.error["message"], envelope.error


def test_line_reader_refuses_an_unbounded_frame():
    """A sender that never sends a newline must not exhaust QGIS's memory."""

    class Endless:
        def recv(self, _size):
            return b"x" * 8192

    reader = LineReader(Endless(), max_frame=20_000)
    with pytest.raises(FrameError) as caught:
        reader.read_line()
    assert "no newline" in str(caught.value), str(caught.value)


def test_line_reader_reports_a_truncated_frame():
    """A peer vanishing mid-message is a failure, not an empty read."""

    chunks = [b'{"command":"pi', b""]

    class Dying:
        def recv(self, _size):
            return chunks.pop(0)

    with pytest.raises(FrameError) as caught:
        LineReader(Dying()).read_line()
    assert "part-way through a frame" in str(caught.value)


def test_max_frame_is_generous_but_bounded():
    assert 100_000 < MAX_FRAME_BYTES <= 10_000_000


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_unknown_command_lists_the_supported_ones(harness):
    """Candidates rather than a bare refusal (requirements.md §0)."""
    with harness.connect() as conversation:
        envelope = conversation.call("no_such_command")
    assert not envelope.ok
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST
    assert "ping" in envelope.error["supported_commands"]


def test_project_state_sees_the_open_project(harness, fixture_truth):
    with harness.connect() as conversation:
        envelope = conversation.call("project_state")
    assert envelope.ok, describe(envelope.error)
    result = envelope.result
    assert result["layer_count"] == fixture_truth.PROJECT_LAYER_COUNT
    assert result["crs"] == fixture_truth.PROJECT_CRS
    names = {layer["name"] for layer in result["layers"]}
    assert names == set(fixture_truth.PROJECT_LAYER_NAMES), sorted(names)


def test_project_state_reports_the_hidden_layer(harness, fixture_truth):
    """Visibility comes from the live layer tree, not the file."""
    with harness.connect() as conversation:
        envelope = conversation.call("project_state")
    hidden = [
        layer["name"] for layer in envelope.result["layers"] if layer["visible"] is False
    ]
    assert hidden == [fixture_truth.PROJECT_HIDDEN_LAYER], hidden


def test_layer_name_mistakes_return_the_available_names(harness):
    with harness.connect() as conversation:
        envelope = conversation.call("zoom_to_layer", layer="not_a_layer")
    assert not envelope.ok
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST
    assert "survey_points" in envelope.error["message"], envelope.error["message"]


def test_canvas_commands_report_the_interface_as_missing(harness):
    """The limit of headless testing, stated as its own assertion.

    These commands need a GUI. Here they must refuse with a code that says so
    -- not a generic failure, which would read as a bug rather than as the
    absence of a desktop.
    """
    with harness.connect() as conversation:
        for command in ("get_screenshot_of_canvas", "get_screenshot_of_window"):
            envelope = conversation.call(command)
            assert not envelope.ok, f"{command} should refuse headlessly"
            assert envelope.error["code"] == ErrorCode.MODE_UNAVAILABLE, (
                f"{command}: {describe(envelope.error)}"
            )
            assert "desktop interface" in envelope.error["message"]


# ---------------------------------------------------------------------------
# REQ-4a: the pre-execution backup
# ---------------------------------------------------------------------------


def test_execution_takes_a_backup_first(harness):
    """REQ-4a: the backup is what justifies skipping REQ-4's gate."""
    with harness.connect() as conversation:
        envelope = conversation.call("execute_pyqgis_code", code="result = 1 + 1")
    assert envelope.ok, describe(envelope.error)
    backup = envelope.result["backup"]
    path = Path(backup["backup_path"])
    assert path.exists(), f"no backup at {path}"
    assert backup["backup_bytes"] > 0


def test_backup_states_what_it_does_not_cover(harness):
    """REQ-4c requires the limits stated, and they travel with the response."""
    with harness.connect() as conversation:
        envelope = conversation.call("execute_pyqgis_code", code="result = 1")
    backup = envelope.result["backup"]
    assert "project structure only" in backup["covers"]
    assert "not reversible" in backup["does_not_cover"]
    assert "GeoTIFF" in backup["does_not_cover"]


def test_the_backup_does_not_repoint_the_project(harness, fixture_project):
    """The trap that would make the backup cause the loss it prevents.

    `QgsProject.write(path)` repoints `fileName()` at the path given. Left
    alone, the session would point into the backup directory and the user's
    next save would overwrite the backup instead of their project.
    """
    with harness.connect() as conversation:
        before = conversation.call("ping").result["project_file"]
        conversation.call("execute_pyqgis_code", code="result = 1")
        after = conversation.call("ping").result["project_file"]

    assert before == after, (
        f"taking a backup changed the project's file name\n"
        f"    before: {before}\n"
        f"    after:  {after}"
    )
    assert Path(after).name == fixture_project.name


def test_the_backup_does_not_clear_unsaved_changes(harness):
    """The other half of the same trap, and the more dangerous half.

    `write()` also clears the dirty flag. A project wrongly believed clean does
    not prompt on close, so the user quits and loses the very work the backup
    was protecting.
    """
    with harness.connect() as conversation:
        # Dirty the project the way agent code would.
        dirtied = conversation.call(
            "execute_pyqgis_code",
            code=(
                "from qgis.core import QgsProject\n"
                "QgsProject.instance().setDirty(True)\n"
                "result = QgsProject.instance().isDirty()"
            ),
        )
        assert dirtied.ok, describe(dirtied.error)
        assert dirtied.result["result"] is True

        # A second execution takes another backup while the project is dirty.
        after = conversation.call("execute_pyqgis_code", code="result = 1")
        assert after.ok, describe(after.error)
        assert after.result["has_unsaved_changes"] is True, (
            "the backup cleared the unsaved-changes flag, so QGIS would not "
            "prompt the user on close and their work would be lost silently"
        )

        # Leave the harness clean for whatever runs next.
        conversation.call(
            "execute_pyqgis_code",
            code="from qgis.core import QgsProject\nQgsProject.instance().setDirty(False)\nresult = 1",
        )


def test_a_syntax_error_does_not_churn_the_backups(harness):
    """Refused before the backup: broken code cannot modify anything."""
    with harness.connect() as conversation:
        before = conversation.call("project_backups").result["count"]
        envelope = conversation.call("execute_pyqgis_code", code="result = (")
        after = conversation.call("project_backups").result["count"]

    assert not envelope.ok
    assert "syntax error" in envelope.error["message"]
    assert after == before, "a syntax error should not consume a backup slot"


def test_code_that_raises_still_reports_the_backup_path(harness):
    """The moment the path matters most is the moment it is easiest to lose.

    A failure that reported only `ValueError: ...` would discard the one piece
    of information a caller whose code just broke actually needs.
    """
    with harness.connect() as conversation:
        envelope = conversation.call(
            "execute_pyqgis_code", code="raise ValueError('deliberate')"
        )
    assert not envelope.ok
    assert envelope.error["code"] == ErrorCode.CODE_RAISED
    assert envelope.error["exception_type"] == "ValueError"
    assert "backup" in envelope.error, describe(envelope.error)
    assert Path(envelope.error["backup"]["backup_path"]).exists()
    assert "saved at" in envelope.error["recovery"]


def test_stdout_is_captured_and_returned(harness):
    with harness.connect() as conversation:
        envelope = conversation.call(
            "execute_pyqgis_code", code="print('live hello')\nresult = 'done'"
        )
    assert envelope.ok, describe(envelope.error)
    assert envelope.result["stdout"].strip() == "live hello"
    assert envelope.result["result"] == "done"


def test_req3_is_enforced_on_the_live_path_too(harness):
    """The same return convention as headless, named the same way."""
    with harness.connect() as conversation:
        envelope = conversation.call(
            "execute_pyqgis_code",
            code=(
                "from qgis.core import QgsRectangle\n"
                "result = {'box': QgsRectangle(0, 0, 1, 1)}"
            ),
        )
    assert not envelope.ok
    assert "result['box']" in envelope.error["message"], envelope.error["message"]
    assert "QgsRectangle" in envelope.error["message"]


def test_code_without_a_result_is_not_an_error(harness):
    with harness.connect() as conversation:
        envelope = conversation.call("execute_pyqgis_code", code="x = 1")
    assert envelope.ok, describe(envelope.error)
    assert envelope.result["result_defined"] is False
    assert envelope.result["result"] is None


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


def test_a_second_client_is_told_why(harness):
    """One connection at a time, and the second is answered rather than hung.

    This was a bug: the accept loop served each connection inline, so while
    one client was attached `accept()` was never reached and a second sat in
    the kernel backlog, connected and never spoken to.
    """
    with harness.connect() as first:
        assert first.call("ping").ok
        with harness.connect() as second:
            envelope = second.read()
            assert not envelope.ok
            assert "already connected" in envelope.error["message"]
        # The first client is unaffected.
        assert first.call("ping").ok


def test_the_slot_frees_when_a_client_disconnects(harness):
    first = harness.connect()
    assert first.call("ping").ok
    first.close()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with harness.connect() as conversation:
            envelope = conversation.call("ping")
            if envelope.ok:
                return
        time.sleep(0.2)
    pytest.fail("the connection slot was not released after a disconnect")


# ---------------------------------------------------------------------------
# The server-side client
# ---------------------------------------------------------------------------


def test_client_reaches_the_harness(harness):
    client = LiveClient(port=harness.port)
    session = client.session(force=True)
    assert session is not None, client.last_error
    assert session.qgis_version.startswith("4."), session.qgis_version
    assert session.layer_count > 0
    assert client.is_reachable() is True


def test_client_reports_an_absent_session_as_mode_unavailable():
    """REQ-1a's contract, from the server's side.

    A port nothing is listening on must produce an error naming the live mode,
    not a transport exception -- the caller's next move depends on knowing
    which mode went missing.
    """
    # Port 1 on loopback: privileged, and nothing in this project binds it.
    client = LiveClient(port=1)
    assert client.is_reachable() is False
    envelope = client.call("ping")
    assert not envelope.ok
    assert envelope.error["code"] == ErrorCode.MODE_UNAVAILABLE
    assert envelope.error["missing_mode"] == ExecutionMode.LIVE
    assert "Start MCP listener" in envelope.error["message"]


def test_client_can_be_disabled():
    """A user who wants the server headless-only with QGIS open."""
    client = LiveClient(port=9999, enabled=False)
    assert client.is_reachable() is False
    assert "disabled" in client.last_error
    envelope = client.call("ping")
    assert envelope.error["code"] == ErrorCode.MODE_UNAVAILABLE


def test_reachability_is_cached(harness, monkeypatch):
    """Every mode-agnostic tool asks on every call; probing each time would
    add a socket connect to tools that never touch the live path."""
    client = LiveClient(port=harness.port)
    assert client.is_reachable(force=True) is True

    calls = {"n": 0}
    original = client._call_once

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "_call_once", counted)
    for _ in range(5):
        client.is_reachable()
    assert calls["n"] == 0, (
        f"a cached answer should need no probe; made {calls['n']}"
    )


def test_invalidate_forces_a_fresh_probe(harness):
    """A stale "reachable" is worse than a slow "no"."""
    client = LiveClient(port=harness.port)
    assert client.is_reachable(force=True) is True
    client.invalidate()
    assert client._session is None
    assert client.is_reachable() is True


# ---------------------------------------------------------------------------
# Mode partition
# ---------------------------------------------------------------------------


def test_live_tools_are_mode_named():
    """REQ-1a's scope, as the requirements list it."""
    for name in (
        "execute_pyqgis_code",
        "get_screenshot_of_canvas",
        "get_screenshot_of_window",
        "zoom_to_layer",
        "set_active_layer",
        "toggle_layer_visibility",
    ):
        assert modes.MODE_NAMED_TOOLS.get(name) == ExecutionMode.LIVE, name
        assert modes.resolve(name).mode == ExecutionMode.LIVE


def test_render_map_to_path_is_still_not_mode_named():
    """The carve-out REQ-1a makes explicitly, which task 6 must not undo."""
    assert "render_map_to_path" not in modes.MODE_NAMED_TOOLS


def test_no_mode_agnostic_tool_claims_live_yet():
    """Honesty about what has a live implementation.

    Listing a tool as live-capable before it has a live path would make its
    reported reason a lie: it would say "no live session is reachable" while a
    session sat open and ignored.
    """
    assert modes.LIVE_CAPABLE_TOOLS == frozenset()
    decision = modes.resolve("get_project_summary", live_available=True)
    assert decision.mode == ExecutionMode.HEADLESS
    assert "no live implementation" in decision.reason


# ---------------------------------------------------------------------------
# The live tools, through the registered surface
# ---------------------------------------------------------------------------


def _registered_against(client) -> CollectingServer:
    """Register the live tools against a client, and nothing else.

    A stand-in context rather than the session's real one: ``register`` reads
    only ``context.live``, and reassigning that on the shared session-scoped
    context would leave every later test in the run talking to a harness port
    that is about to close.
    """
    from types import SimpleNamespace

    from qgis_mcp_server.tools import live as live_module

    server = CollectingServer()
    live_module.register(server, SimpleNamespace(live=client))
    return server


@pytest.fixture
def live_tools(harness):
    """The live tools, pointed at the harness."""
    return _registered_against(LiveClient(port=harness.port))


def test_get_live_session_reports_a_reachable_session(live_tools):
    payload = live_tools.tools["get_live_session"]()
    assert payload["ok"] is True, describe(payload)
    assert payload["live"] is True
    assert payload["qgis_version"].startswith("4.")


def test_get_live_session_is_not_an_error_when_nothing_is_there():
    """"Is QGIS open?" has "no" as a legitimate answer.

    Returning a failure would make it indistinguishable from the check itself
    breaking.
    """
    server = _registered_against(LiveClient(port=1))

    payload = server.tools["get_live_session"]()
    assert payload["ok"] is True, describe(payload)
    assert payload["live"] is False
    assert "Start MCP listener" in payload["remedy"]


def test_execute_tool_round_trips_through_the_socket(live_tools, harness):
    payload = live_tools.tools["execute_pyqgis_code"](
        code="import threading\nresult = threading.get_ident()"
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["result"] == harness.main_thread
    assert payload["mode"] == ExecutionMode.LIVE
    assert Path(payload["backup"]["backup_path"]).exists()


def test_execute_tool_refuses_empty_code(live_tools):
    payload = live_tools.tools["execute_pyqgis_code"](code="   ")
    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST


def test_visibility_tool_passes_false_through(live_tools):
    """`visible=False` means hide, not unspecified.

    Dropping falsy parameters is right for optional numbers and wrong here --
    it would silently turn "hide this" into "toggle this".
    """
    import inspect

    signature = inspect.signature(live_tools.tools["toggle_layer_visibility"])
    assert signature.parameters["visible"].default is None

    payload = live_tools.tools["toggle_layer_visibility"](
        layer="tracks", visible=False
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["visible"] is False
    assert payload["toggled"] is False, (
        "an explicit visible=False must set the state, not toggle it"
    )


def test_canvas_tool_reports_the_missing_interface(live_tools):
    payload = live_tools.tools["get_screenshot_of_canvas"]()
    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.MODE_UNAVAILABLE
    assert payload["mode"] == ExecutionMode.LIVE


def test_backups_tool_lists_what_was_written(live_tools):
    live_tools.tools["execute_pyqgis_code"](code="result = 1")
    payload = live_tools.tools["list_project_backups"](limit=5)
    assert payload["ok"] is True, describe(payload)
    assert payload["count"] >= 1
    assert "manual" in payload["restore"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_live_tools_register_with_their_warnings(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()
    descriptions = {tool.name: tool.description or "" for tool in registered}

    for name in (
        "get_live_session",
        "execute_pyqgis_code",
        "get_screenshot_of_canvas",
        "get_screenshot_of_window",
        "zoom_to_layer",
        "set_active_layer",
        "toggle_layer_visibility",
    ):
        assert name in descriptions, sorted(descriptions)

    execute = descriptions["execute_pyqgis_code"]
    # The three things a caller cannot infer: that it is live-only, that a
    # backup is taken, and what the backup does not protect.
    assert "LIVE ONLY" in execute, execute
    assert "BACKUP IS TAKEN" in execute, execute
    # Asserted on a phrase that cannot straddle the wrap, since the warning
    # being *reachable* is the point and its line breaks are incidental.
    assert "does NOT cover: underlying data" in execute, execute


def test_the_plugin_declares_a_maximum_qgis_version():
    """design.md §6.2: without it, QGIS 4 silently refuses to load the plugin.

    Silently is the problem -- there is no error anywhere obvious, so this is
    worth asserting rather than trusting.
    """
    metadata = (
        Path(__file__).parent.parent / "qgis_mcp_plugin" / "metadata.txt"
    ).read_text(encoding="utf-8")
    assert "qgisMaximumVersion=" in metadata, metadata
    assert "qgisMinimumVersion=4" in metadata, metadata


def test_the_plugin_finds_the_protocol_package_without_the_repo_on_sys_path():
    """The plugin must bootstrap its own import path.

    This was a real failure, and an invisible one: the suite passes because
    ``live_harness.py`` inserts the repo's ``src`` on ``sys.path`` itself, and
    the headless children get it via ``child_env``. A GUI QGIS does neither --
    it adds the profile's plugin directory and nothing else -- so the plugin
    imported ``qgis_mcp_protocol`` successfully in every automated context and
    would have raised ImportError the moment anyone enabled it.

    So this asserts the condition QGIS actually imposes: the package is found
    with the repo absent from ``sys.path``.
    """
    repo = Path(__file__).resolve().parent.parent
    probe = (
        "import sys\n"
        # Strip the repo, exactly as a QGIS process would never have had it.
        f"sys.path = [p for p in sys.path if {str(repo)!r}.lower() not in p.lower()]\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "import importlib.util\n"
        "assert importlib.util.find_spec('qgis_mcp_protocol') is None, 'repo not stripped'\n"
        "import qgis_mcp_plugin\n"
        "from qgis_mcp_protocol.socket_frame import DEFAULT_PORT\n"
        "print(qgis_mcp_plugin.PROTOCOL_SOURCE)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    assert result.returncode == 0, (
        "the plugin could not import the shared protocol package without the "
        f"repo on sys.path, which is the condition a GUI QGIS imposes:\n"
        f"{result.stderr}"
    )
    assert result.stdout.strip(), "the bootstrap did not report where it looked"


def test_the_bootstrap_failure_says_where_it_looked():
    """A plugin that will not load should not say only "No module named ...".

    The Plugin Manager shows the exception and little else, so the message is
    the entire diagnosis the user gets.
    """
    import importlib

    bootstrap = importlib.import_module("qgis_mcp_plugin._bootstrap")
    roots = bootstrap._candidate_roots(
        Path(__file__).resolve().parent.parent / "qgis_mcp_plugin"
    )
    described = [description for _path, description in roots]
    assert any("vendored" in text for text in described), described
    assert any("repo checkout" in text for text in described), described


def test_the_plugin_imports_through_the_qgis_pyqt_shim():
    """design.md §6.2: never PyQt5 or PyQt6 directly.

    PyQt6 scopes its enums, so `Qt.QueuedConnection` became
    `Qt.ConnectionType.QueuedConnection`. The shim is what keeps one source
    working across both bindings.
    """
    plugin_dir = Path(__file__).parent.parent / "qgis_mcp_plugin"
    offenders = []
    for path in plugin_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import PyQt5", "import PyQt6")) or (
                stripped.startswith("from PyQt5") or stripped.startswith("from PyQt6")
            ):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, (
        "these import a binding directly instead of through qgis.PyQt:\n"
        + "\n".join(offenders)
    )
