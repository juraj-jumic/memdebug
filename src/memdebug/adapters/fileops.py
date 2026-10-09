"""The file-level half of a rollback, shared by both rollback engines.

The git engine (restore.py) and the plain-folder engine (folder_restore.py) both inherit `FileOps` from this module.
Nothing here knows about git. It reads, checks, writes and removes files under one root folder, and it is the only code that
does, so the same rules hold for every store type:

  * links, junctions and anything that is not a plain file or folder are never followed and never written through;
  * a file whose name differs from another in the same folder only by letter case is refused;
  * files are written through a temporary file in the same folder and an atomic rename;
  * every write is journaled so that a failed step can be undone, newest first.
"""
from __future__ import annotations

import os
import secrets
import stat
from typing import Sequence

from .. import longpath
from ..errors import RestoreError
from ..textsafe import safe_text
from .markdown_git import MAX_FILE_BYTES, _is_reparse_point, _text_from_bytes

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or _is_reparse_point(info)


class FileOps:
    """Subclasses set `self._root` (the folder every path is relative to) and plan what to write."""

    _root: str | os.PathLike[str]

    def _inspect(self, relpath: str) -> tuple[str, bytes | None]:
        """Reads a working-tree file without following links.

        Returns:
            ("ok", bytes), ("missing", None) when the file or a folder on the way does not exist, ("large", None) when it
            is over MAX_FILE_BYTES, or ("unsafe", None) when a folder on the way or the file itself is a link or
            junction or is not a plain folder or file, or when it cannot be opened or read.
        """
        current = longpath.fs(self._root)
        parts = relpath.split("/")
        for part in parts[:-1]:
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return "missing", None
            except OSError:
                return "unsafe", None
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                return "unsafe", None
        full = os.path.join(current, parts[-1])
        try:
            info = os.lstat(full)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unsafe", None
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            return "unsafe", None
        if info.st_size > MAX_FILE_BYTES:
            return "large", None
        try:
            fd = os.open(full, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return "unsafe", None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "unsafe", None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            return "unsafe", None
        finally:
            os.close(fd)
        return ("large", None) if len(data) > MAX_FILE_BYTES else ("ok", data)

    def _check_target(self, rel: str, must_exist: bool = False) -> None:
        """Checks that `rel` can be written (or, with `must_exist`, removed) without crossing a link.

        Reads only; changes nothing.

        Raises:
            RestoreError: If a folder on the way is a link or not a folder, the file is not a plain file, another file in
                its folder differs from it only by letter case, or (with `must_exist`) a folder or the file is missing.
        """
        current = longpath.fs(self._root)
        parts = rel.split("/")
        for part in parts[:-1]:
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                if must_exist:
                    raise RestoreError(f"{safe_text(rel, 60)}: a folder disappeared") from None
                break
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise RestoreError(f"{safe_text(rel, 60)}: a folder on the way is a link or not a folder")
        else:
            try:
                names = os.listdir(current)
            except OSError as exc:
                raise RestoreError(f"{safe_text(rel, 60)}: cannot read its folder ({exc.strerror})") from exc
            name = parts[-1]
            if name not in names and any(other.casefold() == name.casefold() for other in names):
                raise RestoreError(f"{safe_text(rel, 60)}: another file in that folder differs only by letter case")
            target = os.path.join(current, name)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                if must_exist:
                    raise RestoreError(f"{safe_text(rel, 60)}: the file disappeared") from None
                return
            if _is_link(info) or not stat.S_ISREG(info.st_mode):
                raise RestoreError(f"{safe_text(rel, 60)}: not a plain file")

    def _write_file(self, rel: str, data: bytes) -> list[str]:
        """Replaces or creates a file through a temporary file and an atomic rename.

        Missing folders on the way are created. If anything fails, the temporary file is deleted and any folders created
        here are removed again (if empty) before the error is raised.

        Returns:
            The folders this call created, outermost first.

        Raises:
            RestoreError: If a folder on the way, or the file itself, is a link or not a plain folder or file.
        """
        created: list[str] = []
        try:
            current = longpath.fs(self._root)
            parts = rel.split("/")
            for part in parts[:-1]:
                current = os.path.join(current, part)
                try:
                    info = os.lstat(current)
                except FileNotFoundError:
                    os.mkdir(current)
                    created.append(current)
                    info = os.lstat(current)
                if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                    raise RestoreError(f"{safe_text(rel, 60)}: a folder on the way is a link or not a folder")
            target = os.path.join(current, parts[-1])
            mode = None
            try:
                info = os.lstat(target)
                if _is_link(info) or not stat.S_ISREG(info.st_mode):
                    raise RestoreError(f"{safe_text(rel, 60)}: not a plain file")
                mode = stat.S_IMODE(info.st_mode)
            except FileNotFoundError:
                pass
            temporary = os.path.join(current, f".memdebug-{secrets.token_hex(8)}.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o666)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if mode is not None and os.name == "posix":
                    os.chmod(temporary, mode)
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            return created
        except BaseException:
            for folder in reversed(created):
                try:
                    os.rmdir(folder)
                except OSError:
                    pass
            raise

    def _remove_file(self, rel: str) -> None:
        """Deletes a plain file after `_check_target` confirms it is there and safe to touch."""
        self._check_target(rel, must_exist=True)
        os.unlink(os.path.join(longpath.fs(self._root), *rel.split("/")))

    def _verify_files(self, items: Sequence) -> None:
        """Checks that every item is now on disk exactly as planned.

        Raises:
            RestoreError: If a removed file is still there, or a written file is missing or does not hold the planned text.
        """
        for item in items:
            status, data = self._inspect(item.path)
            if item.action == "remove":
                if status != "missing":
                    raise RestoreError(f"{safe_text(item.path, 60)} is still there after removal")
            elif status != "ok" or _text_from_bytes(data or b"") != item.target_text:
                raise RestoreError(f"{safe_text(item.path, 60)} does not hold the restored text after writing")

    def _undo_files(self, journal: Sequence, created_dirs: Sequence[str]) -> list[str]:
        """Puts the files back, newest step first, then removes the folders that were created.

        Each journal entry is a path and the bytes it held before (None if it did not exist). Failures do not stop
        the undo. Created folders are removed only if they are empty.

        Returns:
            One message for each file that could not be put back; empty when everything was undone.
        """
        problems: list[str] = []
        for path, old in reversed(journal):
            try:
                if old is None:
                    full = os.path.join(longpath.fs(self._root), *path.split("/"))
                    if os.path.lexists(full):
                        os.unlink(full)
                else:
                    self._write_file(path, old)
            except Exception as exc:
                problems.append(f"{safe_text(path, 60)}: {safe_text(exc, 80)}")
        for folder in reversed(created_dirs):
            try:
                os.rmdir(folder)
            except OSError:
                pass
        return problems

