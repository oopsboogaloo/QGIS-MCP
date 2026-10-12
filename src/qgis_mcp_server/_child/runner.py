"""Runs one batch of agent-authored code inside a standalone QGIS, then exits.

This file executes in the **child** process, under the QGIS-bundled interpreter.
It is never imported by the server (``spec/design.md`` §2.2: the server process
never imports PyQGIS), and it is launched by path rather than with ``-m``, so
the ``qgis_mcp_server`` package is not on its import path and cannot drag server
dependencies into an interpreter that does not have them.

Imports are limited to the standard library, ``qgis.*``, and the shared
``qgis_mcp_protocol`` package staged alongside this file.

**Why the code arrives as data.** ``design.md`` §3.2 calls for "a generated
standalone PyQGIS script", which invites templating the agent's code into a
source file. This runner takes it as a JSON request instead, for three reasons
that only show up once something goes wrong:

* Escaping. Agent-authored code contains quotes, backslashes and triple-quoted
  strings. Every templating scheme has a payload that breaks it.
* Tracebacks. ``exec`` of a named code object reports line numbers relative to
  the submitted snippet. Templated code reports them relative to the generated
  file, so the caller is told about a line it never wrote.
* Boundaries. With code as data there is one parser -- ``json`` -- between the
  processes, rather than a bespoke quoting convention on each side.

Usage: ``python runner.py <request.json>``
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

#: Name the submitted code is compiled under, so tracebacks point somewhere
#: recognisable rather than at a temp path the caller never saw.
CODE_FILENAME = "<agent-code>"

#: REQ-3: data comes back by assignment to this name.
RESULT_NAME = "result"


def _emit(envelope) -> None:
    """Write one envelope to stdout and flush.

    Flushed explicitly: the process is about to call ``exitQgis``, and QGIS
    teardown has been known to take the interpreter down hard enough to lose a
    buffered write -- which would reach the server as "no envelope" and be
    misreported as a crash.
    """
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def _json_safe(value, *, path="result"):
    """Check that ``value`` will survive the boundary, per REQ-3.

    Returns ``(True, value)`` or ``(False, description_of_the_offender)``. The
    description names the *path* to the bad object rather than just its type,
    because the common failure is one PyQGIS object nested inside an otherwise
    fine dict, and "not serialisable" alone does not say which key to fix.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            # NaN and infinities are not valid JSON. json.dumps emits them as
            # bare NaN/Infinity tokens, which many strict parsers reject -- so
            # they are caught here rather than shipped as a payload that may or
            # may not decode at the other end.
            return False, f"{path} is the non-JSON float {value!r}"
        return True, value
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            good, detail = _json_safe(item, path=f"{path}[{index}]")
            if not good:
                return False, detail
        return True, value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return False, (
                    f"{path} has a non-string key {key!r} "
                    f"({type(key).__name__}); JSON object keys must be strings"
                )
            good, detail = _json_safe(item, path=f"{path}[{key!r}]")
            if not good:
                return False, detail
        return True, value
    return False, (
        f"{path} is a {type(value).__name__}, which is not JSON-serialisable. "
        f"REQ-3 requires {RESULT_NAME} to hold plain data -- convert PyQGIS "
        f"objects to their values first (e.g. layer.name(), "
        f"feature.attributes(), geometry.asWkt())."
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"runner expects exactly one argument, the request file path; "
                f"got {len(argv) - 1}",
                mode=ExecutionMode.HEADLESS,
            )
        )
        return 2

    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"could not read request file {argv[1]}: {exc}",
                mode=ExecutionMode.HEADLESS,
            )
        )
        return 2

    code = request.get("code")
    if not isinstance(code, str):
        _emit(
            error(
                ErrorCode.BAD_REQUEST,
                f"request field 'code' must be a string, got "
                f"{type(code).__name__}",
                mode=ExecutionMode.HEADLESS,
            )
        )
        return 2

    prefix_path = request.get("prefix_path") or os.environ.get("QGIS_PREFIX_PATH")
    project_path = request.get("project_path")

    # Compile before starting QGIS. A syntax error is the agent's, not the
    # environment's, and paying a QGIS startup to report one is pure latency.
    try:
        compiled = compile(code, CODE_FILENAME, "exec")
    except SyntaxError as exc:
        _emit(
            error(
                ErrorCode.CODE_RAISED,
                f"submitted code has a syntax error: {exc.msg} "
                f"(line {exc.lineno}, offset {exc.offset})",
                mode=ExecutionMode.HEADLESS,
                exception_type="SyntaxError",
                line=exc.lineno,
                offset=exc.offset,
                text=(exc.text or "").rstrip() or None,
            )
        )
        return 1

    from qgis.core import QgsApplication, QgsProject

    if prefix_path:
        QgsApplication.setPrefixPath(str(prefix_path), True)
    app = QgsApplication([], False)
    app.initQgis()

    exit_code = 0
    try:
        meta: dict = {}

        if project_path:
            project = QgsProject.instance()
            if not project.read(str(project_path)):
                _emit(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"could not read project file: {project_path}",
                        mode=ExecutionMode.HEADLESS,
                        project_path=str(project_path),
                    )
                )
                return 1
            meta["project_path"] = str(project_path)
            meta["project_layer_count"] = len(project.mapLayers())

        namespace: dict = {"__name__": "__agent__", "__builtins__": __builtins__}
        captured = io.StringIO()

        # Agent code is free to print. Capturing it keeps the envelope's
        # surroundings clean and hands the output back deliberately, instead of
        # leaving it in the diagnostics stream mixed with GDAL and Qt chatter.
        try:
            with contextlib.redirect_stdout(captured):
                exec(compiled, namespace)
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            stdout = captured.getvalue()
            _emit(
                error(
                    ErrorCode.CODE_RAISED,
                    f"{type(exc).__name__}: {exc}",
                    mode=ExecutionMode.HEADLESS,
                    exception_type=type(exc).__name__,
                    traceback=traceback.format_exc(),
                    stdout=stdout or None,
                )
            )
            return 1

        stdout = captured.getvalue()
        if stdout:
            meta["stdout"] = stdout

        if RESULT_NAME not in namespace:
            # Not an error. Plenty of useful calls mutate state and return
            # nothing, and failing them would push callers into writing
            # `result = None` as boilerplate. The flag is what lets a caller
            # tell "returned nothing" from "returned null".
            meta["result_defined"] = False
            _emit(ok(None, mode=ExecutionMode.HEADLESS, **meta))
            return 0

        value = namespace[RESULT_NAME]
        good, detail = _json_safe(value)
        if not good:
            _emit(
                error(
                    ErrorCode.RESULT_NOT_SERIALISABLE,
                    detail,
                    mode=ExecutionMode.HEADLESS,
                    result_type=type(value).__name__,
                    stdout=stdout or None,
                )
            )
            return 1

        meta["result_defined"] = True
        _emit(ok(value, mode=ExecutionMode.HEADLESS, **meta))
    finally:
        # Always run, including after an early return above: a child that skips
        # exitQgis can leave provider connections and lock files behind, which
        # shows up later as a mysteriously unreadable fixture.
        app.exitQgis()

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
