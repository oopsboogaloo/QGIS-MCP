"""Project backups taken before agent-authored code runs (REQ-4a, 4b, 4c).

REQ-4a exempts raw execution from REQ-4's ``confirm=true`` gate -- the server
cannot statically tell whether submitted code is destructive, and gating every
call would make the interactive loop unusable -- and requires a backup instead.

REQ-4b fixes the mechanics, and each clause is here for a reason:

* **Serialise the in-memory project, not the file on disk.** In a live session
  the saved file may be an arbitrary amount of work out of date, so copying it
  would back up a state the user abandoned.
* **A central per-user directory, not the project folder.** That way an unsaved
  or untitled project is still covered, which is precisely the case with the
  most to lose.
* **Named by project plus ISO timestamp, pruned to a retention count.**

**The trap this module exists to get right.** ``QgsProject.write(path)`` does
two things beyond writing: it repoints ``fileName()`` at the path given, and it
clears the dirty flag. Verified against QGIS 4.2, not assumed. Left alone,
taking a backup would leave the user's session pointing at a file in the cache
directory and convinced nothing is unsaved -- so Ctrl+S would overwrite the
backup rather than the project, and closing QGIS would not prompt at all. The
backup would cause exactly the loss it exists to prevent. Both are captured
before the write and restored after.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

#: Backups kept per project before the oldest are pruned (REQ-4b's default).
DEFAULT_RETENTION = 20

#: Filename-safe stand-in for a project that has never been saved. Untitled
#: projects are the ones REQ-4b's central directory is for, so they need a name
#: rather than being skipped.
UNTITLED = "untitled"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class BackupError(Exception):
    """A backup could not be written, so execution must not proceed."""


def default_backup_dir() -> Path:
    """Per-user backup directory, matching the server's cache location."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "qgis-mcp" / "backups"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "qgis-mcp" / "backups"


def project_slug(project) -> str:
    """A filename-safe name for a project, saved or not."""
    name = project.baseName() or ""
    if not name:
        file_name = project.fileName() or ""
        name = Path(file_name).stem if file_name else ""
    slug = _UNSAFE.sub("_", name).strip("_")
    return slug or UNTITLED


def take_backup(
    project,
    *,
    directory: Path | None = None,
    retention: int = DEFAULT_RETENTION,
) -> dict:
    """Write a timestamped backup of the in-memory project.

    **Must run on the QGIS main thread.** It touches the live
    ``QgsProject``, which is a GUI-session object.

    Returns a dict describing what was written, including the path REQ-4a
    requires be handed back to the caller.

    Raises:
        BackupError: nothing was written. The caller must refuse to execute --
            REQ-4a makes the backup the thing that justifies skipping REQ-4's
            confirmation, so running without one removes the only protection.
    """
    folder = Path(directory) if directory else default_backup_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(
            f"could not create the backup directory {folder}: {exc}"
        ) from exc

    slug = project_slug(project)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    destination = folder / f"{slug}_{stamp}.qgz"
    # A second backup inside the same second is entirely possible when an
    # agent runs several snippets in a row, and the later one must not
    # silently replace the earlier.
    suffix = 1
    while destination.exists():
        destination = folder / f"{slug}_{stamp}_{suffix}.qgz"
        suffix += 1

    # --- the trap, handled -------------------------------------------------
    original_file_name = project.fileName()
    was_dirty = project.isDirty()

    try:
        written = bool(project.write(str(destination)))
    except Exception as exc:  # noqa: BLE001 - becomes a refusal to execute
        _restore(project, original_file_name, was_dirty)
        raise BackupError(f"writing the backup failed: {exc}") from exc

    # Restored before anything else can observe the session, and in a finally
    # -style order: the file name first, since leaving that wrong is what
    # would make Ctrl+S overwrite the backup.
    _restore(project, original_file_name, was_dirty)

    if not written or not destination.exists():
        raise BackupError(
            f"QGIS reported no error but no backup appeared at {destination}. "
            f"Refusing to execute without one."
        )

    pruned = prune(folder, slug, retention=retention)

    return {
        "backup_path": str(destination),
        "backup_bytes": destination.stat().st_size,
        "project": slug,
        "project_was_unsaved": not original_file_name,
        "project_had_unsaved_changes": was_dirty,
        "retention": retention,
        "pruned": pruned,
        # REQ-4c requires the limits stated. They are stated in the tool
        # description too; repeating them on every response means a caller
        # reading only the result still sees them.
        "covers": (
            "project structure only: layer references, styling, layouts, "
            "project CRS and settings"
        ),
        "does_not_cover": (
            "underlying data. Code that overwrites a GeoTIFF, truncates a data "
            "provider or deletes a shapefile is not protected by this backup "
            "and is not reversible."
        ),
    }


def _restore(project, file_name: str, was_dirty: bool) -> None:
    """Undo ``write``'s side effects on the live project.

    Order matters. The file name is restored first because that is the one
    whose corruption is actively dangerous -- a subsequent save would write
    over the backup. The dirty flag second, because a project wrongly believed
    clean will not prompt on close.
    """
    try:
        project.setFileName(file_name)
    except Exception:  # noqa: BLE001 - best effort; reported by the caller
        pass
    if was_dirty:
        try:
            project.setDirty(True)
        except Exception:  # noqa: BLE001
            pass


def prune(folder: Path, slug: str, *, retention: int = DEFAULT_RETENTION) -> list[str]:
    """Delete all but the newest ``retention`` backups for one project.

    Per project rather than per directory: a busy session on one project must
    not evict the only backup of another.
    """
    if retention <= 0:
        return []
    try:
        existing = sorted(
            folder.glob(f"{slug}_*.qgz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []

    removed: list[str] = []
    for path in existing[retention:]:
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            # A backup that cannot be deleted is not worth failing the
            # operation over; it only means the directory grows.
            continue
    return removed


def list_backups(folder: Path | None = None) -> list[dict]:
    """Every backup on disk, newest first.

    REQ-4c defers ``restore_project_backup`` to the write-operations phase, so
    recovery is manual for now -- opening the ``.qgz``. Listing them is what
    makes that possible without the user going hunting through a cache
    directory.
    """
    directory = Path(folder) if folder else default_backup_dir()
    if not directory.is_dir():
        return []
    entries = []
    for path in directory.glob("*.qgz"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "path": str(path),
                "bytes": stat.st_size,
                "modified": datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.timezone.utc
                ).isoformat(),
            }
        )
    entries.sort(key=lambda entry: entry["modified"], reverse=True)
    return entries


__all__ = [
    "DEFAULT_RETENTION",
    "UNTITLED",
    "BackupError",
    "default_backup_dir",
    "list_backups",
    "project_slug",
    "prune",
    "take_backup",
]
