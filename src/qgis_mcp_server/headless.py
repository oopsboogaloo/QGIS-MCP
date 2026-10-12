"""The headless execution path: QGIS as a child process.

REQ-2 requires requests to execute without the QGIS GUI open, and
``spec/design.md`` §2.2 requires the server to reach QGIS only across a process
boundary. This module is the join between those two: it stages a dependency-free
payload, builds the child environment, launches the QGIS-bundled interpreter,
and reads one structured envelope back.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

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

#: Where the child-side runner lives in this package.
RUNNER_SOURCE = Path(__file__).parent / "_child" / "runner.py"


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
        """Copy the runner and the shared protocol package to a temp directory.

        **Why copy rather than point at the installed packages.** The obvious
        move is to put the server's ``site-packages`` on the child's
        ``PYTHONPATH`` so it can import ``qgis_mcp_protocol`` in place. That
        would also expose every other distribution in the server's virtualenv
        to an interpreter that ships its own -- and QGIS bundles its own numpy
        and GDAL bindings, compiled against its own libraries. A second numpy
        arriving earlier on ``sys.path`` is a segfault or an import error deep
        inside GDAL, at a remove from anything that would point here.

        So the child gets a directory containing exactly two things, both pure
        standard library by construction (§2.3). Nothing of the server's
        environment is visible to it.
        """
        if self._staged is not None and self._staged.exists():
            return self._staged

        staged = Path(tempfile.mkdtemp(prefix="qgis-mcp-child-"))
        shutil.copy2(RUNNER_SOURCE, staged / "runner.py")
        protocol_dir = Path(qgis_mcp_protocol.__file__).parent
        shutil.copytree(
            protocol_dir,
            staged / "qgis_mcp_protocol",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self._staged = staged
        return staged

    def run_code(
        self,
        code: str,
        *,
        project_path: Path | str | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Execute ``code`` in a standalone QGIS and return its envelope.

        Never raises for an execution failure -- every outcome comes back as an
        envelope, so a caller handles one shape rather than branching between
        exceptions and return values.
        """
        try:
            install = self.config.install
        except QgisNotFound as exc:
            # REQ-1a: this tool names its mode, so an unavailable headless path
            # is an explicit error naming the missing mode -- never a silent
            # hand-off to the live session, which would run the agent's code
            # somewhere it did not ask for.
            return error(
                ErrorCode.MODE_UNAVAILABLE,
                f"headless execution is unavailable: {exc}",
                mode=ExecutionMode.HEADLESS,
                missing_mode=ExecutionMode.HEADLESS,
            )

        resolved_project = project_path or self.config.project_path
        if resolved_project is not None:
            resolved_project = Path(resolved_project).expanduser()
            if not resolved_project.exists():
                return error(
                    ErrorCode.BAD_REQUEST,
                    f"project file does not exist: {resolved_project}",
                    mode=ExecutionMode.HEADLESS,
                    project_path=str(resolved_project),
                )

        staged = self._stage()
        effective_timeout = (
            timeout if timeout is not None else self.config.headless_timeout
        )

        request = {
            "code": code,
            "prefix_path": str(install.prefix_path),
            "project_path": str(resolved_project) if resolved_project else None,
        }

        with tempfile.TemporaryDirectory(prefix="qgis-mcp-req-") as workdir:
            request_path = Path(workdir) / "request.json"
            request_path.write_text(
                json.dumps(request), encoding="utf-8"
            )

            try:
                completed = subprocess.run(
                    [
                        str(install.python_exe),
                        str(staged / "runner.py"),
                        str(request_path),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=effective_timeout,
                    env=install.child_env(extra_pythonpath=[staged]),
                    cwd=workdir,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                partial = _as_text(exc.stdout)
                return error(
                    ErrorCode.TIMEOUT,
                    f"headless execution exceeded {effective_timeout:.0f}s and "
                    f"was terminated. Raise the timeout for long Processing "
                    f"runs, or move the work into run_processing_algorithm.",
                    mode=ExecutionMode.HEADLESS,
                    timeout_seconds=effective_timeout,
                    partial_output=partial.strip()[-4000:] or None,
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
                ErrorCode.NO_ENVELOPE if completed.returncode == 0 else ErrorCode.CHILD_FAILED,
                f"the QGIS child process produced no usable result: {exc}",
                mode=ExecutionMode.HEADLESS,
                exit_code=completed.returncode,
                stdout=(completed.stdout or "").strip()[-4000:] or None,
                stderr=(completed.stderr or "").strip()[-4000:] or None,
            )

        return envelope


def _as_text(value: bytes | str | None) -> str:
    """Normalise the mixed-type output ``TimeoutExpired`` carries."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


__all__ = ["HeadlessRunner", "RUNNER_SOURCE"]
