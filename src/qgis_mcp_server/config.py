"""Server configuration, and the startup validation that makes it trustworthy.

Deliberately small. Only settings the implemented tools actually read live here
-- the config surface grows with the build order rather than ahead of it, so
that an unused key never looks like a supported one.

Precedence, highest first: explicit constructor arguments, environment
variables, ``qgis-mcp.toml`` in the working directory, defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qgis_mcp_server.credentials import CredentialStore
from qgis_mcp_server.qgis_install import ENV_QGIS_ROOT, QgisInstall, QgisNotFound, locate

CONFIG_FILENAME = "qgis-mcp.toml"

ENV_PROJECT_PATH = "QGIS_MCP_PROJECT"
ENV_HEADLESS_TIMEOUT = "QGIS_MCP_HEADLESS_TIMEOUT"
ENV_CACHE_DIR = "QGIS_MCP_CACHE_DIR"

#: Default ceiling on a headless child. Generous because a legitimate
#: Processing run over a large raster takes minutes (``design.md`` §3.1 makes
#: the same point about the live socket timeout), and a timeout that fires on
#: real work is worse than no timeout at all -- it teaches the caller to
#: distrust the tool rather than the data.
DEFAULT_HEADLESS_TIMEOUT = 300.0


def default_cache_dir() -> Path:
    """Per-user cache directory for geocoder resolutions.

    Central rather than beside a project, for the same reason REQ-4b puts
    backups in a central location: a resolution is about a place, not about one
    project, and caching it per project would re-query Nominatim for the same
    village every time a new project opens.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "qgis-mcp" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "qgis-mcp"


class ConfigError(Exception):
    """Configuration is malformed or self-contradictory."""


@dataclass
class Config:
    """Resolved settings for one server process."""

    #: Explicit QGIS install root. ``None`` means discover.
    qgis_root: Path | None = None
    #: Project the headless path operates against (REQ-2). Optional, because
    #: plenty of useful calls -- algorithm search, documentation lookup, a bare
    #: expression -- need no project at all.
    project_path: Path | None = None
    headless_timeout: float = DEFAULT_HEADLESS_TIMEOUT
    #: The ``[credentials]`` table, provider name to key. Never logged or
    #: returned -- see ``credentials.CredentialStore`` for why the values stay
    #: there and only their status travels.
    credentials: dict[str, Any] = field(default_factory=dict)
    #: Where recorded geocoder responses live. Keeping Nominatim usage inside
    #: its policy depends on this being persistent between runs.
    cache_dir: Path = field(default_factory=default_cache_dir)

    _install: QgisInstall | None = None
    _credential_store: CredentialStore | None = None

    @classmethod
    def load(
        cls,
        *,
        config_file: Path | None = None,
        qgis_root: Path | str | None = None,
        project_path: Path | str | None = None,
        headless_timeout: float | None = None,
    ) -> Config:
        """Resolve configuration from arguments, environment and file."""
        file_values = _read_config_file(config_file)

        resolved_root = (
            qgis_root
            or os.environ.get(ENV_QGIS_ROOT)
            or file_values.get("qgis", {}).get("root")
        )
        resolved_project = (
            project_path
            or os.environ.get(ENV_PROJECT_PATH)
            or file_values.get("project", {}).get("path")
        )

        timeout = headless_timeout
        if timeout is None:
            raw = os.environ.get(ENV_HEADLESS_TIMEOUT) or file_values.get(
                "headless", {}
            ).get("timeout")
            if raw is not None:
                try:
                    timeout = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(
                        f"headless timeout must be a number, got {raw!r}"
                    ) from exc
        if timeout is None:
            timeout = DEFAULT_HEADLESS_TIMEOUT
        if timeout <= 0:
            raise ConfigError(
                f"headless timeout must be positive, got {timeout}"
            )

        return cls(
            qgis_root=Path(resolved_root).expanduser() if resolved_root else None,
            project_path=(
                Path(resolved_project).expanduser() if resolved_project else None
            ),
            headless_timeout=timeout,
            credentials=dict(file_values.get("credentials") or {}),
            cache_dir=Path(
                os.environ.get(ENV_CACHE_DIR)
                or (file_values.get("geocoding", {}) or {}).get("cache_dir")
                or default_cache_dir()
            ).expanduser(),
        )

    @property
    def credential_store(self) -> CredentialStore:
        """Resolves provider keys from the environment and this config."""
        if self._credential_store is None:
            self._credential_store = CredentialStore(self.credentials)
        return self._credential_store

    @property
    def install(self) -> QgisInstall:
        """The resolved QGIS install, located once and cached.

        Raises :class:`QgisNotFound` with a usable diagnostic. Callers on the
        tool path should prefer :meth:`validate`, which is called at startup so
        that a misconfiguration is reported before any tool is invoked rather
        than as a surprise during one (Task 1 acceptance).
        """
        if self._install is None:
            self._install = locate(self.qgis_root)
        return self._install

    def validate(self, *, probe: bool = False) -> dict[str, Any]:
        """Check the configuration and report what was found.

        Structural checks are cheap and always run. ``probe`` additionally
        starts QGIS to read back its real version and registered Processing
        providers -- authoritative, but it costs a QGIS startup, so it belongs
        in ``--check`` rather than on every launch.

        Raises :class:`QgisNotFound` or :class:`ConfigError` on anything fatal.
        Returns a report for display; warnings are collected rather than raised,
        since a missing *project* file does not stop the server from being
        useful.
        """
        install = self.install
        report: dict[str, Any] = {
            "qgis_root": str(install.root),
            "qgis_label": install.label,
            "version_hint": install.version_hint,
            "prefix_path": str(install.prefix_path),
            "python_exe": str(install.python_exe),
            "qgis_process_exe": str(install.qgis_process_exe),
            "bindings_dir": str(install.bindings_dir),
            "env_file": str(install.env_file) if install.env_file else None,
            "headless_timeout": self.headless_timeout,
            "project_path": str(self.project_path) if self.project_path else None,
            "cache_dir": str(self.cache_dir),
            "warnings": [],
        }

        if self.project_path is not None and not self.project_path.exists():
            report["warnings"].append(
                f"configured project file does not exist: {self.project_path}"
            )

        if self.qgis_root is None:
            report["warnings"].append(
                f"QGIS install was discovered, not configured. With several "
                f"versions installed this may not be the one you meant -- set "
                f"{ENV_QGIS_ROOT} to pin it."
            )

        # Reported as presence and source only. A validation report is
        # printed to a terminal and may be pasted into an issue, so the key
        # itself must not be in it.
        from qgis_mcp_server.providers.catalogue import ALL_CREDENTIALS

        report["credentials"] = [
            self.credential_store.status(spec).to_dict() for spec in ALL_CREDENTIALS
        ]

        if probe:
            probed = install.probe()
            report["probe"] = probed
            report["qgis_version"] = probed.get("qgis_version")
            report["python_version"] = probed.get("python_version")
            report["qt_version"] = probed.get("qt_version")
            report["gdal_version"] = probed.get("gdal_version")
            # design.md §6.4: report what is actually registered. A provider
            # that exists but did not load is the case a hardcoded list gets
            # wrong, so surface it here where it is cheap to see.
            plugins = probed.get("plugins") or {}
            report["providers_loaded"] = sorted(
                name for name, info in plugins.items() if info.get("loaded")
            )
            report["providers_present_not_loaded"] = sorted(
                name for name, info in plugins.items() if not info.get("loaded")
            )

        return report


def _read_config_file(config_file: Path | None) -> dict[str, Any]:
    """Read ``qgis-mcp.toml``, tolerating its absence but not its malformation.

    A missing file is the normal case -- discovery plus environment variables
    cover most installs. A *present but broken* file is an error, because
    silently ignoring it would leave the user looking at settings that are not
    in effect.
    """
    path = config_file or Path.cwd() / CONFIG_FILENAME
    if not path.exists():
        if config_file is not None:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_HEADLESS_TIMEOUT",
    "Config",
    "ConfigError",
    "QgisNotFound",
]
