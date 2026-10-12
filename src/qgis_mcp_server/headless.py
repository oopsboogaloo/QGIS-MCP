"""The headless execution path: QGIS as a child process.

REQ-2 requires requests to execute without the QGIS GUI open, and
``spec/design.md`` §2.2 requires the server to reach QGIS only across a process
boundary. This module is the join between those two: it stages a dependency-free
payload, builds the child environment, launches the QGIS-bundled interpreter,
and reads one structured envelope back.

Every child works the same way -- a script in ``_child/`` receives a JSON
request file path as its only argument and writes one sentinel-delimited
envelope to stdout -- so :meth:`HeadlessRunner.run_child` serves all of them and
each new capability is a child script rather than new launch machinery.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import qgis_mcp_protocol
from qgis_mcp_protocol import (
    Envelope,
    EnvelopeError,
    ErrorCode,
    ExecutionMode,
    error,
    extract,
)
from qgis_mcp_server.config import Config
from qgis_mcp_server.qgis_install import QgisNotFound

#: Directory holding the child-side scripts.
CHILD_SOURCE_DIR = Path(__file__).parent / "_child"

#: The script that executes agent-authored code (task 1).
RUNNER_SCRIPT = "runner.py"

#: The script that answers read-only introspection queries (task 2).
INTROSPECT_SCRIPT = "introspect.py"


@dataclass
class HeadlessRunner:
    """Launches QGIS children for one server process.

    Holds a staged payload directory for its lifetime, so the per-call cost is
    a request file and a process launch rather than a directory copy.
    """

    config: Config
    _staged: Path | None = None

    def close(self) -> None:
        """Remove the staged payload directory."""
        if self._staged is not None and self._staged.exists():
            shutil.rmtree(self._staged, ignore_errors=True)
        self._staged = None

    def _stage(self) -> Path:
        """Copy the child scripts and the shared protocol package to a temp dir.

        **Why copy rather than point at the installed packages.** The obvious
        move is to put the server's ``site-packages`` on the child's
        ``PYTHONPATH`` so it can import ``qgis_mcp_protocol`` in place. That
        would also expose every other distribution in the server's virtualenv
        to an interpreter that ships its own -- and QGIS bundles its own numpy
        and GDAL bindings, compiled against its own libraries. A second numpy
        arriving earlier on ``sys.path`` is a segfault or an import error deep
        inside GDAL, at a remove from anything that would point here.

        So the child gets a directory containing only the child scripts and the
        protocol package, which is pure standard library by construction
        (§2.3). Nothing of the server's environment is visible to it.
        """
        if self._staged is not None and self._staged.exists():
            return self._staged

        staged = Path(tempfile.mkdtemp(prefix="qgis-mcp-child-"))
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        for script in CHILD_SOURCE_DIR.glob("*.py"):
            shutil.copy2(script, staged / script.name)
        shutil.copytree(
            Path(qgis_mcp_protocol.__file__).parent,
            staged / "qgis_mcp_protocol",
            ignore=ignore,
        )
        self._staged = staged
        return staged

    # -- the generic child call -------------------------------------------

    def run_child(
        self,
        script: str,
        request: dict[str, Any],
        *,
        timeout: float | None = None,
        timeout_hint: str = "",
    ) -> Envelope:
        """Run one child script against ``request`` and return its envelope.

        ``prefix_path`` is added to the request here rather than by each
        caller, since every child needs it to call ``setPrefixPath`` and none
        of them should have to know where it came from.

        Never raises for an execution failure -- every outcome comes back as an
        envelope, so a caller handles one shape rather than branching between
        exceptions and return values.
        """
        try:
            install = self.config.install
        except QgisNotFound as exc:
            return error(
                ErrorCode.QGIS_NOT_FOUND,
                f"headless execution is unavailable: {exc}",
                mode=ExecutionMode.HEADLESS,
            )

        staged = self._stage()
        effective_timeout = (
            timeout if timeout is not None else self.config.headless_timeout
        )
        payload = dict(request)
        payload["prefix_path"] = str(install.prefix_path)

        with tempfile.TemporaryDirectory(prefix="qgis-mcp-req-") as workdir:
            request_path = Path(workdir) / "request.json"
            request_path.write_text(json.dumps(payload), encoding="utf-8")

            try:
                completed = subprocess.run(
                    [
                        str(install.python_exe),
                        str(staged / script),
                        str(request_path),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=effective_timeout,
                    env=install.child_env(extra_pythonpath=[staged]),
                    # The child inherits the server's working directory on
                    # purpose. Running it in the request's temp directory would
                    # silently change what a relative path means between the
                    # two processes -- a path the server resolved and found
                    # would not resolve in the child.
                    cwd=os.getcwd(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return error(
                    ErrorCode.TIMEOUT,
                    f"headless execution exceeded {effective_timeout:.0f}s and "
                    f"was terminated."
                    + (f" {timeout_hint}" if timeout_hint else ""),
                    mode=ExecutionMode.HEADLESS,
                    timeout_seconds=effective_timeout,
                    partial_output=_as_text(exc.stdout).strip()[-4000:] or None,
                )
            except OSError as exc:
                # The interpreter path validated at startup but will not launch
                # -- a broken install, or a security product blocking it.
                return error(
                    ErrorCode.QGIS_NOT_FOUND,
                    f"could not launch the QGIS interpreter at "
                    f"{install.python_exe}: {exc}",
                    mode=ExecutionMode.HEADLESS,
                    python_exe=str(install.python_exe),
                )

        combined = (completed.stdout or "") + (
            f"\n{completed.stderr}" if completed.stderr else ""
        )

        try:
            envelope, _ = extract(combined)
        except EnvelopeError as exc:
            # No envelope means the child died before the runner could report.
            # The captured output is the whole diagnosis here, so it travels
            # with the error rather than being summarised away -- this is the
            # §6.1 path-misconfiguration failure as the user actually meets it.
            return error(
                ErrorCode.NO_ENVELOPE
                if completed.returncode == 0
                else ErrorCode.CHILD_FAILED,
                f"the QGIS child process produced no usable result: {exc}",
                mode=ExecutionMode.HEADLESS,
                script=script,
                exit_code=completed.returncode,
                stdout=(completed.stdout or "").strip()[-4000:] or None,
                stderr=(completed.stderr or "").strip()[-4000:] or None,
            )

        return envelope

    # -- project resolution -----------------------------------------------

    def resolve_project(
        self, project_path: Path | str | None
    ) -> tuple[Path | None, Envelope | None]:
        """Resolve a project path to an absolute one, or explain why not.

        **Absolute, not merely expanded.** The server validates that the file
        exists, and the child is what opens it. If a relative path crossed the
        boundary unresolved, the two processes could disagree about which file
        it names the moment either one's working directory differed -- the
        server would confirm a file it found and the child would fail to open
        one it never saw. Resolving here makes the path mean one thing.
        """
        resolved = project_path or self.config.project_path
        if resolved is None:
            return None, None
        path = Path(resolved).expanduser()
        if not path.exists():
            return None, error(
                ErrorCode.BAD_REQUEST,
                f"project file does not exist: {path}",
                mode=ExecutionMode.HEADLESS,
                project_path=str(path),
            )
        return path.resolve(), None

    # -- task 1: raw execution --------------------------------------------

    def run_code(
        self,
        code: str,
        *,
        project_path: Path | str | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Execute ``code`` in a standalone QGIS and return its envelope."""
        try:
            self.config.install
        except QgisNotFound as exc:
            # REQ-1a: this tool names its mode, so an unavailable headless path
            # is an explicit error naming the missing mode -- never a silent
            # hand-off to the live session, which would run the agent's code
            # somewhere it did not ask for. Distinct from the generic
            # QGIS_NOT_FOUND that run_child returns for mode-agnostic callers.
            return error(
                ErrorCode.MODE_UNAVAILABLE,
                f"headless execution is unavailable: {exc}",
                mode=ExecutionMode.HEADLESS,
                missing_mode=ExecutionMode.HEADLESS,
            )

        resolved_project, failure = self.resolve_project(project_path)
        if failure is not None:
            return failure

        return self.run_child(
            RUNNER_SCRIPT,
            {
                "code": code,
                "project_path": str(resolved_project) if resolved_project else None,
            },
            timeout=timeout,
            timeout_hint=(
                "Raise the timeout for long Processing runs, or move the work "
                "into run_processing_algorithm."
            ),
        )


def _as_text(value: bytes | str | None) -> str:
    """Normalise the mixed-type output ``TimeoutExpired`` carries."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


__all__ = [
    "CHILD_SOURCE_DIR",
    "INTROSPECT_SCRIPT",
    "RUNNER_SCRIPT",
    "HeadlessRunner",
]
