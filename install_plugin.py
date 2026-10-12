"""Install the listener plugin into a QGIS profile.

Run with the server's Python; it imports nothing outside the standard library
and never touches PyQGIS.

    python install_plugin.py                 # link to this checkout (dev)
    python install_plugin.py --copy          # self-contained copy
    python install_plugin.py --list          # show profiles and status
    python install_plugin.py --uninstall

Two things this exists to get right.

**The profile path.** QGIS 4 keeps an entirely separate tree from 3.x --
``QGIS4``, not ``QGIS3`` -- and a plugin placed in the 3.x path simply never
appears in the Plugin Manager. That failure looks exactly like a broken plugin,
which makes it worth not typing by hand.

**The shared protocol package.** The plugin imports ``qgis_mcp_protocol``,
which lives in ``src/`` and is not on a GUI QGIS's ``sys.path``. A linked
install lets the plugin find it in the checkout; ``--copy`` vendors it into the
plugin directory instead. See ``qgis_mcp_plugin/_bootstrap.py``.

A link is the default because this is a plugin under active development: an
edit to the repo takes effect on the next QGIS start with nothing to re-copy,
and there is no second copy to drift. ``--copy`` is for handing it to someone
else.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
PLUGIN_SOURCE = REPO / "qgis_mcp_plugin"
PROTOCOL_SOURCE = REPO / "src" / "qgis_mcp_protocol"

#: The plugin directory name inside the profile. Must match the package name
#: the plugin is imported as, since QGIS imports it by directory name.
PLUGIN_NAME = "qgis_mcp_plugin"

#: Files never worth copying into a profile.
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "_vendor")


def profile_roots() -> list[Path]:
    """Candidate QGIS profile trees, newest major version first.

    Both majors are listed because a machine can have 3.x and 4.x side by side
    and they share nothing. The plugin needs QGIS 4.2+, so a 3.x profile is
    reported and refused rather than silently installed into.
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        base = base / "QGIS"
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support/QGIS"
    else:
        base = Path.home() / ".local/share/QGIS"
    return [base / "QGIS4", base / "QGIS3"]


def discover_profiles() -> list[tuple[Path, int]]:
    """Every profile directory found, with the QGIS major version it belongs to."""
    found: list[tuple[Path, int]] = []
    for root in profile_roots():
        major = 4 if root.name.endswith("4") else 3
        profiles = root / "profiles"
        if not profiles.is_dir():
            continue
        for entry in sorted(profiles.iterdir()):
            if entry.is_dir():
                found.append((entry, major))
    return found


def plugin_dir(profile: Path) -> Path:
    return profile / "python" / "plugins" / PLUGIN_NAME


def _describe(path: Path) -> str:
    """What is at ``path``, distinguishing a link from a copy."""
    if not path.exists():
        return "not installed"
    if path.is_symlink():
        return f"linked -> {os.readlink(path)}"
    try:
        # A Windows junction is not a symlink to Python, but resolves away.
        if path.resolve() != path:
            return f"junction -> {path.resolve()}"
    except OSError:
        pass
    vendored = (path / "_vendor" / "qgis_mcp_protocol").is_dir()
    return "copy (protocol vendored)" if vendored else "copy"


def _link(source: Path, target: Path) -> str:
    """Link ``target`` to ``source``, by whatever means the platform allows.

    On Windows a directory junction is tried first: unlike a symlink it needs
    no administrator rights and no Developer Mode, so the ordinary case does
    not require an elevated shell.
    """
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "junction"
        try:
            target.symlink_to(source, target_is_directory=True)
            return "symlink"
        except OSError as exc:
            raise SystemExit(
                f"could not link {target} to {source}.\n"
                f"  mklink said: {(result.stderr or result.stdout).strip()}\n"
                f"  symlink said: {exc}\n"
                f"Use --copy instead, which needs no special permissions."
            ) from exc
    target.symlink_to(source, target_is_directory=True)
    return "symlink"


def install(profile: Path, *, copy: bool, force: bool) -> None:
    target = plugin_dir(profile)
    if target.exists() or target.is_symlink():
        if not force:
            raise SystemExit(
                f"{target} already exists ({_describe(target)}).\n"
                f"Re-run with --force to replace it, or --uninstall first."
            )
        remove(profile, quiet=True)

    target.parent.mkdir(parents=True, exist_ok=True)

    if copy:
        shutil.copytree(PLUGIN_SOURCE, target, ignore=SKIP)
        vendor = target / "_vendor" / "qgis_mcp_protocol"
        vendor.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PROTOCOL_SOURCE, vendor, ignore=SKIP)
        how = "copied, with qgis_mcp_protocol vendored"
    else:
        how = _link(PLUGIN_SOURCE, target) + f" to {PLUGIN_SOURCE}"

    print(f"installed: {target}\n  {how}")
    print(
        "\nNext, in QGIS:\n"
        "  1. Plugins -> Manage and Install Plugins -> Installed\n"
        "  2. tick 'QGIS MCP'\n"
        "  3. Plugins -> QGIS MCP -> Start MCP listener\n"
        "\nIf QGIS is already running, restart it first -- it scans the plugin\n"
        "directory at startup."
    )


def remove(profile: Path, *, quiet: bool = False) -> None:
    target = plugin_dir(profile)
    if not target.exists() and not target.is_symlink():
        if not quiet:
            print(f"nothing installed at {target}")
        return
    # Order matters: a junction must be removed as a directory entry, never
    # walked -- rmtree through a junction would delete the repo it points at.
    if target.is_symlink():
        target.unlink()
    elif os.name == "nt" and target.resolve() != target:
        os.rmdir(target)
    else:
        shutil.rmtree(target)
    if not quiet:
        print(f"removed: {target}")


def show() -> None:
    profiles = discover_profiles()
    if not profiles:
        print("no QGIS profiles found. Start QGIS once to create one.")
        return
    print(f"{'profile':<52} {'QGIS':<6} plugin")
    for profile, major in profiles:
        note = _describe(plugin_dir(profile))
        if major < 4:
            note += "  (QGIS 3 profile -- this plugin needs 4.2+)"
        print(f"{str(profile):<52} {major}.x    {note}")


def pick_profile(requested: str | None) -> Path:
    profiles = discover_profiles()
    if requested:
        path = Path(requested).expanduser()
        if path.is_dir():
            return path
        matches = [p for p, _ in profiles if p.name == requested]
        if not matches:
            names = ", ".join(sorted({p.name for p, _ in profiles})) or "(none)"
            raise SystemExit(
                f"no profile named {requested!r}. Found: {names}\n"
                f"Pass a full path, or --list to see them."
            )
        return matches[0]

    usable = [path for path, major in profiles if major >= 4]
    if not usable:
        raise SystemExit(
            "no QGIS 4 profile found. The plugin needs QGIS 4.2 or newer "
            "(spec/design.md section 6.2).\n"
            "Start QGIS 4 once to create a profile, then re-run. --list shows "
            "what was found."
        )
    if len(usable) > 1:
        names = ", ".join(path.name for path in usable)
        print(f"note: several QGIS 4 profiles ({names}); using {usable[0].name}")
    return usable[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", help="profile name or full path")
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy the plugin and vendor the protocol package, instead of linking",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing install")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--list", action="store_true", help="show profiles and exit")
    args = parser.parse_args(argv)

    if args.list:
        show()
        return 0

    if not PLUGIN_SOURCE.is_dir():
        raise SystemExit(f"{PLUGIN_SOURCE} is missing; run this from the repo")

    profile = pick_profile(args.profile)
    if args.uninstall:
        remove(profile)
    else:
        install(profile, copy=args.copy, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
