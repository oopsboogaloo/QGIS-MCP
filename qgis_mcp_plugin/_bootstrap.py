"""Make ``qgis_mcp_protocol`` importable inside QGIS's own Python.

The plugin shares the envelope and framing contract with the server rather than
restating it (``design.md`` §2.3), which means it has to import a package that
lives outside the plugin directory. Every other consumer of that package gets
it handed to them:

* the server imports it from its own virtualenv;
* the headless children get it staged into a temp dir and put on ``PYTHONPATH``
  by ``child_env(extra_pythonpath=...)``;
* ``tests/live_harness.py`` inserts the repo's ``src`` on ``sys.path`` itself.

A plugin loaded by a GUI QGIS gets none of that. QGIS adds the *profile's*
plugin directory to ``sys.path`` and nothing else, so without this the first
``from qgis_mcp_protocol import ...`` fails and the symptom is a plugin that
will not enable, reported as a bare ``ImportError`` in the Plugin Manager with
no indication of what is actually missing.

So the plugin finds the package itself, and says where it found it. Two install
shapes are supported deliberately, because they suit different uses:

**Junction or symlink to the repo** -- what you want while developing. The
package is found by walking up from this file's *resolved* path to the repo's
``src``, so an edit to the shared contract takes effect on the next QGIS start
with nothing to re-copy.

**A self-contained copy** -- what you want to hand to someone else. ``_vendor/``
next to this file holds a copy of the package, which ``install_plugin.py
--copy`` writes. Nothing outside the plugin directory is then required.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

#: Name of the shared package this plugin needs.
PACKAGE = "qgis_mcp_protocol"

#: Where a self-contained install keeps its copy.
VENDOR_DIRNAME = "_vendor"

#: How far up to look for a repo checkout. The plugin normally sits at
#: ``<repo>/qgis_mcp_plugin``, so one level finds ``<repo>/src``; the extra
#: levels cost nothing and cover a nested layout.
SEARCH_DEPTH = 4


class ProtocolNotFound(ImportError):
    """The shared protocol package could not be located.

    Raised with the places that were tried, because the useful question when a
    plugin will not load is "where did it look", and an ``ImportError`` naming
    only the module answers a different one.
    """


def _candidate_roots(here: Path) -> list[tuple[Path, str]]:
    """Directories that might contain the package, with how each was reached."""
    candidates: list[tuple[Path, str]] = [
        (here / VENDOR_DIRNAME, "vendored copy inside the plugin")
    ]
    for level, parent in enumerate(here.parents):
        if level >= SEARCH_DEPTH:
            break
        candidates.append((parent / "src", f"repo checkout at {parent}"))
    return candidates


def ensure_protocol_importable() -> str:
    """Put the shared package on ``sys.path`` if it is not already there.

    Returns a short description of where it came from, for the log line the
    plugin writes on load -- so an install that works for an unexpected reason
    is visible rather than silent.

    Raises:
        ProtocolNotFound: with every location tried.
    """
    if importlib.util.find_spec(PACKAGE) is not None:
        return "already importable"

    # Resolved, not raw: on a junction or symlink install this is what turns
    # the profile path into the real repo path, and the repo is where ``src``
    # is going to be found.
    here = Path(__file__).resolve().parent

    tried: list[str] = []
    for root, description in _candidate_roots(here):
        if (root / PACKAGE / "__init__.py").is_file():
            entry = str(root)
            if entry not in sys.path:
                # Appended rather than prepended: this must not take
                # precedence over a copy QGIS or the user has already arranged,
                # and shadowing an installed package from a plugin is how you
                # get two incompatible copies of a contract in one process.
                sys.path.append(entry)
            return description
        tried.append(str(root / PACKAGE))

    raise ProtocolNotFound(
        f"the QGIS MCP plugin could not find the shared {PACKAGE!r} package, "
        f"which it needs to speak to the server. Looked in:\n"
        + "\n".join(f"  - {path}" for path in tried)
        + "\n\nEither install the plugin with install_plugin.py (which links "
        "to the repo, or vendors a copy with --copy), or put the repo's src "
        "directory on PYTHONPATH before starting QGIS."
    )


__all__ = ["PACKAGE", "ProtocolNotFound", "ensure_protocol_importable"]
