"""Locating a QGIS install and describing it well enough to launch children.

``spec/design.md`` §6.1 names PyQGIS path setup as the single largest source of
failure in standalone PyQGIS tooling, and §2.2 confines the problem rather than
solving it: because the server never imports PyQGIS itself, it does not need the
bindings to be importable *here* -- it needs to construct an environment in
which they are importable *there*. This module is that construction, plus the
validation that makes a misconfiguration say so at startup instead of surfacing
as an opaque ``ImportError`` at the first tool call (Task 1 acceptance).

**Why the Windows path reads an env file.** OSGeo4W installs ship
``bin/qgis-bin.env``: a flat ``VAR=VALUE`` dump of the exact environment QGIS
runs itself under, written at install time, naming the real locations of GDAL,
PROJ, Qt plugins and the bundled interpreter. Reconstructing all of that by hand
means re-deriving decisions the installer already made and recorded -- so this
reads the file and applies the single documented addition that
``bin/python-qgis.bat`` makes on top of it: prepending the PyQGIS bindings to
``PYTHONPATH``. The file is the install's own account of itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Minimum supported QGIS. Set by ``spec/design.md`` §6.2: 4.0 shipped the
#: Qt5-to-Qt6 migration and 4.2 is the first 4.x LTR, so the project targets 4.2
#: rather than the end of the closing 3.x series. Qt6 enum scoping and the
#: ``qgisMaximumVersion`` metadata requirement both follow from this floor.
MIN_QGIS_VERSION = (4, 2)

#: Environment variable overriding install discovery.
ENV_QGIS_ROOT = "QGIS_MCP_QGIS_ROOT"


class QgisNotFound(Exception):
    """No usable QGIS install.

    The message is the product here. Every raise site names what was looked for,
    where it was looked for, and what to do about it, because this is the
    exception a first-time user is most likely to meet.
    """


@dataclass(frozen=True)
class QgisInstall:
    """A validated QGIS install, with everything needed to launch a child.

    Constructed only through :meth:`from_root` or :func:`locate`, both of which
    validate before returning -- so holding one of these is itself the evidence
    that the paths exist.
    """

    root: Path
    #: ``QGIS_PREFIX_PATH`` -- the ``apps/qgis`` directory on OSGeo4W, the
    #: install prefix itself on a POSIX package.
    prefix_path: Path
    #: The QGIS-bundled interpreter. PyQGIS is SIP-compiled against a specific
    #: QGIS build and Python version, so this interpreter and no other.
    python_exe: Path
    #: The ``qgis_process`` CLI. Preferred over bootstrapping ``QgsApplication``
    #: wherever an operation is a Processing algorithm (``design.md`` §3.2).
    qgis_process_exe: Path
    #: Directory holding the ``qgis`` package -- prepended to the child's
    #: ``PYTHONPATH``.
    bindings_dir: Path
    #: OSGeo4W's recorded environment dump, when present.
    env_file: Path | None
    #: Human-readable identity, used in diagnostics. With several installs side
    #: by side, saying which one was chosen is most of the diagnostic value.
    label: str

    @property
    def version_hint(self) -> tuple[int, ...] | None:
        """Version parsed from the install path, if it is there to parse.

        A hint, not an answer: it costs nothing, while :meth:`probe` costs a
        QGIS startup. Used to reject an obviously too-old install and to pick
        between candidates before paying for a probe.
        """
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", self.root.name)
        if not match:
            return None
        return tuple(int(part) for part in match.groups() if part is not None)

    @classmethod
    def from_root(cls, root: Path | str) -> QgisInstall:
        """Validate an install root and describe it.

        Raises :class:`QgisNotFound` naming the first thing that was missing.
        """
        root = Path(root).expanduser()
        if not root.exists():
            raise QgisNotFound(
                f"configured QGIS root does not exist: {root}\n"
                f"Set {ENV_QGIS_ROOT} or the [qgis] root key in qgis-mcp.toml "
                f"to a QGIS {MIN_QGIS_VERSION[0]}.{MIN_QGIS_VERSION[1]} "
                f"install directory."
            )
        if not root.is_dir():
            raise QgisNotFound(
                f"configured QGIS root is not a directory: {root}"
            )

        layout = _windows_layout(root) if os.name == "nt" else _posix_layout(root)
        if layout is None:
            raise QgisNotFound(
                f"{root} does not look like a QGIS install.\n"
                f"Expected to find "
                f"{'apps/qgis/python (the PyQGIS bindings)' if os.name == 'nt' else 'share/qgis/python or lib/python3/dist-packages/qgis'} "
                f"beneath it. If this is the right directory, the install may "
                f"be incomplete -- reinstall QGIS, or point at the directory "
                f"containing bin/ and apps/."
            )

        install = cls(root=root, label=root.name, **layout)
        install._validate_parts()
        return install

    def _validate_parts(self) -> None:
        """Check each path individually, so the message names the missing one.

        A single "install looks wrong" would send the reader hunting. Naming
        the exact missing file usually identifies the cause outright -- a
        partial install, a 3.x layout, or a path pointing one level too high.
        """
        checks = (
            (self.prefix_path, "QGIS_PREFIX_PATH directory"),
            (self.bindings_dir, "PyQGIS bindings directory"),
            (self.bindings_dir / "qgis", "qgis Python package"),
            (self.python_exe, "QGIS-bundled Python interpreter"),
            (self.qgis_process_exe, "qgis_process CLI"),
        )
        missing = [(path, what) for path, what in checks if not path.exists()]
        if missing:
            detail = "\n".join(f"  - {what}: {path}" for path, what in missing)
            raise QgisNotFound(
                f"QGIS install at {self.root} is missing required components:\n"
                f"{detail}\n"
                f"This usually means the path points at the wrong directory "
                f"level, or at a QGIS 3.x install whose layout differs."
            )

        hint = self.version_hint
        if hint is not None and hint[:2] < MIN_QGIS_VERSION:
            found = ".".join(str(part) for part in hint)
            required = ".".join(str(part) for part in MIN_QGIS_VERSION)
            raise QgisNotFound(
                f"QGIS {found} at {self.root} is older than the required "
                f"{required}.\n"
                f"The project targets {required} deliberately: QGIS 4.0 was the "
                f"Qt5-to-Qt6 migration and {required} is the first 4.x LTR, so "
                f"Qt6 is assumed throughout (spec/design.md section 6.2)."
            )

    def child_env(self, *, extra_pythonpath: list[Path] | None = None) -> dict[str, str]:
        """Build the environment a child process needs to import PyQGIS.

        This is the whole of what §2.2 trades for: the server's own environment
        stays ordinary, and the awkwardness is confined here.

        ``extra_pythonpath`` is prepended ahead of the bindings. The shared
        protocol package travels this way, which is what lets the child speak
        the same envelope contract as the server without vendoring a copy.
        """
        if os.name == "nt" and self.env_file is not None and self.env_file.exists():
            env = _read_env_file(self.env_file)
            # Inherit what the recorded dump does not carry -- a user's
            # TEMP/APPDATA, and any provider API keys already in the ambient
            # environment -- without letting it override what QGIS recorded.
            for key, value in os.environ.items():
                env.setdefault(key, value)
        else:
            env = dict(os.environ)
            env["QGIS_PREFIX_PATH"] = str(self.prefix_path)
            if os.name != "nt":
                lib = str(self.root / "lib")
                for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
                    env[var] = os.pathsep.join(filter(None, [lib, env.get(var, "")]))

        # Prepend the bindings, exactly as bin/python-qgis.bat does on top of
        # the recorded environment. Without this the child imports a bare
        # interpreter and `import qgis.core` fails -- the classic §6.1 failure.
        parts = [str(path) for path in (extra_pythonpath or [])]
        parts.append(str(self.bindings_dir))
        existing = env.get("PYTHONPATH", "")
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(parts)

        # Deterministic, parseable child output. QGIS_PREFIX_PATH is reasserted
        # because the POSIX branch above is the only one that set it.
        env.setdefault("QGIS_PREFIX_PATH", str(self.prefix_path))
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Keep a stray local qgis-mcp.toml or sitecustomize out of the child.
        env.pop("PYTHONSTARTUP", None)
        return env

    def probe(self, *, timeout: float = 120.0) -> dict:
        """Ask the install what it actually is, by running ``qgis_process``.

        The authoritative version and -- per ``design.md`` §6.4 -- the set of
        Processing providers that are *really registered*, which is not the same
        as the set that exists. ``grass:*`` needs GRASS present and ``saga:*``
        has not been a core provider since 3.30, so a hardcoded list would
        misreport both.

        Costs a full QGIS startup, so it belongs in ``--check`` and in test
        setup, not on the startup path of every server launch.
        """
        import json

        try:
            completed = subprocess.run(
                [str(self.qgis_process_exe), "--json", "plugins"],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise QgisNotFound(
                f"qgis_process at {self.qgis_process_exe} did not respond "
                f"within {timeout:.0f}s. The install may be broken, or a "
                f"first run may be rebuilding caches -- try again, then "
                f"launch the QGIS GUI once to confirm it starts."
            ) from exc

        if completed.returncode != 0:
            raise QgisNotFound(
                f"qgis_process at {self.qgis_process_exe} exited "
                f"{completed.returncode}.\n"
                f"stderr: {completed.stderr.strip()[:2000] or '(empty)'}"
            )

        start = completed.stdout.find("{")
        if start == -1:
            raise QgisNotFound(
                f"qgis_process produced no JSON.\n"
                f"stdout: {completed.stdout.strip()[:2000] or '(empty)'}"
            )
        try:
            return json.loads(completed.stdout[start:])
        except json.JSONDecodeError as exc:
            raise QgisNotFound(
                f"could not parse qgis_process JSON output: {exc}\n"
                f"stdout: {completed.stdout.strip()[:2000]}"
            ) from exc


def _windows_layout(root: Path) -> dict | None:
    """Describe an OSGeo4W-style install, or ``None`` if this is not one."""
    bindings = root / "apps" / "qgis" / "python"
    prefix = root / "apps" / "qgis"
    if not bindings.is_dir():
        # QGIS ships a long-term-release variant under a different app name.
        bindings = root / "apps" / "qgis-ltr" / "python"
        prefix = root / "apps" / "qgis-ltr"
        if not bindings.is_dir():
            return None

    # PYTHONEXECUTABLE in qgis-bin.env names bin/python3.exe; bin/python.exe is
    # the same interpreter. Prefer what the install declares, accept the other.
    python_exe = root / "bin" / "python3.exe"
    if not python_exe.exists():
        python_exe = root / "bin" / "python.exe"

    qgis_process = prefix / "bin" / "qgis_process.exe"
    if not qgis_process.exists():
        qgis_process = root / "bin" / "qgis_process.exe"

    env_file = root / "bin" / "qgis-bin.env"
    return {
        "prefix_path": prefix,
        "python_exe": python_exe,
        "qgis_process_exe": qgis_process,
        "bindings_dir": bindings,
        "env_file": env_file if env_file.exists() else None,
    }


def _posix_layout(root: Path) -> dict | None:
    """Describe a POSIX install, or ``None`` if this is not one.

    Covers the common Linux package layouts and the macOS app bundle. Less
    exercised than the Windows path -- the structural validation in
    :meth:`QgisInstall._validate_parts` is what keeps a wrong guess here
    legible rather than mysterious.
    """
    candidates = [
        root / "share" / "qgis" / "python",
        root / "lib" / "python3" / "dist-packages",
        root / "lib" / "python3" / "site-packages",
    ]
    bindings = next((path for path in candidates if (path / "qgis").is_dir()), None)
    if bindings is None:
        return None

    qgis_process = root / "bin" / "qgis_process"
    return {
        "prefix_path": root,
        # POSIX packages build PyQGIS against the system interpreter rather
        # than bundling one, so the running interpreter is the correct choice.
        "python_exe": Path(sys.executable),
        "qgis_process_exe": qgis_process,
        "bindings_dir": bindings,
        "env_file": None,
    }


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse OSGeo4W's ``VAR=VALUE`` environment dump.

    Values may contain ``=`` (paths, connection strings), so the split is on
    the first separator only. Blank and comment lines are tolerated; this file
    is installer-generated and its exact shape is not contractual.
    """
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            env[key] = value.strip()
    return env


def default_search_roots() -> list[Path]:
    """Where to look for QGIS when nothing is configured."""
    if os.name == "nt":
        roots: list[Path] = []
        for base in (Path("C:/Program Files"), Path("C:/Program Files (x86)"), Path("C:/")):
            if not base.is_dir():
                continue
            try:
                roots.extend(
                    entry
                    for entry in base.iterdir()
                    if entry.is_dir()
                    and (
                        entry.name.lower().startswith("qgis")
                        or entry.name.lower().startswith("osgeo4w")
                    )
                )
            except OSError:
                continue
        return roots
    return [
        Path("/usr"),
        Path("/usr/local"),
        Path("/Applications/QGIS.app/Contents/MacOS"),
        Path("/Applications/QGIS-LTR.app/Contents/MacOS"),
    ]


def discover_installs() -> list[QgisInstall]:
    """Every usable install found in the default locations, newest first.

    Candidates that fail validation are dropped silently -- this is discovery,
    where an unusable directory is simply not a candidate. A *configured* root
    that fails validation raises instead, because there the user named it.
    """
    found: list[QgisInstall] = []
    for root in default_search_roots():
        try:
            found.append(QgisInstall.from_root(root))
        except QgisNotFound:
            continue
    found.sort(key=lambda install: install.version_hint or (0,), reverse=True)
    return found


def locate(configured_root: Path | str | None = None) -> QgisInstall:
    """Resolve the install to use.

    An explicitly configured root is honoured or raises -- never silently
    replaced by a discovered one, since a typo quietly corrected into a
    *different QGIS version* is exactly the confusion this module exists to
    prevent.
    """
    if configured_root:
        return QgisInstall.from_root(configured_root)

    env_root = os.environ.get(ENV_QGIS_ROOT)
    if env_root:
        return QgisInstall.from_root(env_root)

    installs = discover_installs()
    if not installs:
        searched = "\n".join(f"  - {path}" for path in default_search_roots())
        required = ".".join(str(part) for part in MIN_QGIS_VERSION)
        raise QgisNotFound(
            f"no QGIS {required}+ install found. Searched:\n{searched}\n"
            f"Set {ENV_QGIS_ROOT} or the [qgis] root key in qgis-mcp.toml to "
            f"your install directory."
        )
    return installs[0]
