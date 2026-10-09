"""Listing and removing the backups a plain-folder rollback leaves behind.

A rollback of a plain folder saves what it replaces under `backups/<store>/<date>-<id>/` next to the ledger, and memdebug never deletes
those copies by itself. This module finds them, says how big and how old they are, and removes chosen ones when the person asks.

Removing files is the one dangerous thing here, so the rules are strict:

* Only folders that look exactly like what a rollback creates are ever listed or removed: `<store>/<stamp>` two levels below the backup
  folder, with the names a rollback produces. Anything else in the folder is reported and left alone.
* A link or junction is never followed. A backup that contains one is not removed (a rollback never makes one), and a listing never
  descends into one.
* The age comes from the folder's name, never from a manifest or a file date, because a file's own text and dates are not trusted.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import longpath
from .errors import MemdebugError

_STORE_DIR = re.compile(r"(?!\.+\Z)[A-Za-z0-9._-]{1,60}\Z")  # not "." or "..": a folder name only, never a way up
_STAMP = re.compile(r"([0-9]{8}T[0-9]{6}Z)-[0-9a-f]{8}\Z")
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a symbolic link or a junction on Windows


class BackupError(MemdebugError):
    """A backup could not be listed or removed. The message says whether anything was removed."""


@dataclass(frozen=True)
class Backup:
    """One backup folder made by a rollback.

    Attributes:
        store: The folder name of the store it belongs to (a store name with unusual characters replaced).
        name: The folder's own name, `<date>T<time>Z-<id>`.
        created: When it was made, read from the name.
        files: How many saved files it holds (not counting its manifest), counted, not read from the manifest.
        size: Bytes held by every file in it.
        has_link: True if a link or junction was found inside it. Such a backup is shown but never removed.
    """

    store: str
    name: str
    created: datetime
    files: int
    size: int
    has_link: bool

    def path(self, root: Path) -> Path:
        """Where it is, below the backup folder `root`."""
        return root / self.store / self.name


@dataclass(frozen=True)
class Listing:
    """What a backup folder holds.

    Attributes:
        backups: The backups, newest first.
        ignored: How many entries were not recognised as backups. They are never touched.
    """

    backups: list[Backup]
    ignored: int


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _lstat(path: Path | str) -> os.stat_result | None:
    try:
        return longpath.lstat(path)
    except OSError:
        return None


def _is_real_dir(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat.S_ISDIR(info.st_mode) and not _is_link(info)


def _walk(folder: Path) -> tuple[list[Path], list[Path], bool]:
    """Every file and folder below `folder`, without following links.

    Returns:
        The files, the folders (children before parents is not guaranteed), and whether a link was met. A link is neither listed nor entered.
    """
    files: list[Path] = []
    folders: list[Path] = []
    linked = False
    pending = [folder]
    while pending:
        current = pending.pop()
        try:
            names = longpath.listdir(current)
        except OSError:
            continue
        for name in names:
            child = current / name
            info = _lstat(child)
            if info is None:
                continue
            if _is_link(info):
                linked = True
            elif stat.S_ISDIR(info.st_mode):
                folders.append(child)
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                files.append(child)
    return files, folders, linked


def _created(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_backups(root: Path) -> Listing:
    """Finds the backups below `root`, the backup folder next to the ledger. Reads nothing but names and sizes, and changes nothing.

    Args:
        root: The folder holding one folder per store. It need not exist.
    """
    found: list[Backup] = []
    ignored = 0
    if not longpath.exists(root):
        return Listing([], 0)
    try:
        stores = longpath.listdir(root)
    except OSError as exc:
        raise BackupError(f"the backup folder cannot be read ({exc.strerror or 'unknown reason'})") from exc
    for store in stores:
        store_path = root / store
        if not _STORE_DIR.match(store) or not _is_real_dir(store_path):
            ignored += 1
            continue
        try:
            stamps = longpath.listdir(store_path)
        except OSError:
            ignored += 1
            continue
        for stamp in stamps:
            matched = _STAMP.match(stamp)
            created = _created(matched.group(1)) if matched else None
            path = store_path / stamp
            if created is None or not _is_real_dir(path):
                ignored += 1
                continue
            files, _, linked = _walk(path)
            size = 0
            for file in files:
                info = _lstat(file)
                size += info.st_size if info is not None else 0
            saved = sum(1 for file in files if file.parent != path or file.name != "manifest.json")  # the manifest is not a saved note
            found.append(Backup(store, stamp, created, saved, size, linked))
    found.sort(key=lambda b: (b.created, b.store, b.name), reverse=True)
    return Listing(found, ignored)


def select(backups: list[Backup], *, now: datetime, older_than_days: int | None = None, keep: int | None = None, store: str | None = None,
           everything: bool = False) -> list[Backup]:
    """Chooses the backups a clean-up would remove.

    A backup is chosen only if it meets every condition given. With nothing given, nothing is chosen.

    Args:
        backups: What `list_backups` found.
        now: The current time (given, so tests need not wait).
        older_than_days: Only backups made more than this many days ago.
        keep: Never choose the newest this-many backups of each store.
        store: Only backups of this store (its folder name).
        everything: Choose every backup (of the store, if one is given). Cannot be combined with the other conditions.

    Returns:
        The chosen backups, newest first.

    Raises:
        BackupError: If `everything` is combined with another condition.
    """
    if everything and (older_than_days is not None or keep is not None):
        raise BackupError("--all cannot be combined with --older-than or --keep")
    if not everything and older_than_days is None and keep is None:
        return []
    pool = [b for b in backups if store is None or b.store == store]
    chosen = set(pool)
    if keep is not None:
        chosen = set()
        for name in {b.store for b in pool}:
            ordered = sorted((b for b in pool if b.store == name), key=lambda b: (b.created, b.name), reverse=True)
            chosen.update(ordered[keep:])
    if older_than_days is not None:
        cutoff = now - timedelta(days=older_than_days)
        chosen = {b for b in chosen if b.created < cutoff}
    return [b for b in backups if b in chosen]


def remove(root: Path, backup: Backup) -> int:
    """Removes one backup folder and what is in it, and the store's folder if that is now empty.

    The path is rebuilt from the backup's two names, which are checked again here, and must lie inside `root` with no link below it (a link in `root` itself is the person's own choice).
    Files are removed with `unlink` and folders with `rmdir`, neither of which follows a link, so even a link swapped in after the check
    cannot make this touch anything outside the backup.

    Args:
        root: The folder holding one folder per store.
        backup: One entry of `list_backups`.

    Returns:
        The number of files removed.

    Raises:
        BackupError: If the names are not a rollback's, the folder is not where it should be, it holds a link, or a removal fails. The
            message says whether some files were already removed.
    """
    if not _STORE_DIR.match(backup.store) or not _STAMP.match(backup.name):
        raise BackupError("not a backup folder name; nothing was removed")
    target = backup.path(root)
    expected = os.path.normcase(os.path.join(longpath.realpath(root), backup.store, backup.name))
    if os.path.normcase(longpath.realpath(target)) != expected or not _is_real_dir(target):
        raise BackupError("the backup is not where a rollback puts it (a link on the way?); nothing was removed")
    files, folders, linked = _walk(target)
    if linked:
        raise BackupError("the backup holds a link, which a rollback never creates; left alone, nothing was removed")
    removed = 0
    try:
        for file in files:
            os.unlink(longpath.fs(file))
            removed += 1
        for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
            os.rmdir(longpath.fs(folder))
        os.rmdir(longpath.fs(target))
    except OSError as exc:
        raise BackupError(f"removal stopped ({exc.strerror or 'unknown reason'}) after {removed} file(s); the rest of the backup is still there") from exc
    try:
        os.rmdir(longpath.fs(root / backup.store))  # only succeeds when the store's folder is empty
    except OSError:
        pass
    return removed
