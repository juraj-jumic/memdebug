"""File paths longer than 259 characters on Windows.

Windows refuses a path longer than 259 characters unless the person has switched long paths on in the registry, which is off by default. A
deep note path, a long project folder name (Claude Code names a project folder after the project's whole path) or a backup folder placed
beside the ledger gets there easily. Without care, memdebug would not see such a note at all: it would be left out of the baseline and of every
check, and a rollback would say "nothing to restore" while the note stayed tampered.

So every place that opens, lists, writes or removes a memory file hands the operating system the extended-length form of the path
(`\\\\?\\C:\\...`), which has no such limit and needs no setting. `fs()` produces it. The prefix switches off Windows' tidying of a path, so it
is built from a fully normalised path (backslashes, no `.` or `..`), and a path that comes from elsewhere, such as the `.git/...` names git prints,
is joined first and converted last. Anything shown to a person, stored in the settings or given to a program as its working folder stays in the
ordinary form: `plain()` takes the prefix off, and git cannot be started in a folder beyond the limit at all.

Nothing here changes any other platform: `fs()` returns the path unchanged there.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def fs(path: str | os.PathLike[str], *, _windows: bool | None = None) -> str:
    """The form of a path to hand to the operating system: on Windows its extended-length form, elsewhere the path itself.

    `_windows` exists so the rules can be tested on any platform; it is never set by product code."""
    text = os.fspath(path)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if not (os.name == "nt" if _windows is None else _windows) or text.startswith(("\\\\?\\", "\\\\.\\")):
        return text
    full = ntpath.abspath(text)
    if full.startswith("\\\\"):  # a network path: \\server\share\... becomes \\?\UNC\server\share\...
        return _UNC_PREFIX + full[2:]
    return _PREFIX + full


def plain(path: str | os.PathLike[str]) -> str:
    """The ordinary form of a path: `fs()` undone. Paths without the prefix are returned as they are."""
    text = os.fspath(path)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if text.startswith(_UNC_PREFIX):
        return "\\\\" + text[len(_UNC_PREFIX):]
    if text.startswith(_PREFIX) and len(text) > 6 and text[5] == ":":  # \\?\C:\... (a drive path)
        return text[len(_PREFIX):]
    return text


def resolve(path: str | os.PathLike[str], *, strict: bool = False) -> Path:
    """`Path(path).resolve(strict=strict)`, working beyond the limit; the result is in the ordinary form. Raises OSError like resolve does."""
    if os.name != "nt":
        return Path(path).resolve(strict=strict)
    return Path(plain(os.path.realpath(fs(path), strict=strict)))


def realpath(path: str | os.PathLike[str]) -> str:
    """`os.path.realpath`, working beyond the limit; the result is in the ordinary form."""
    return plain(os.path.realpath(fs(path))) if os.name == "nt" else os.path.realpath(path)


def stat(path: str | os.PathLike[str]) -> os.stat_result:
    return os.stat(fs(path))


def lstat(path: str | os.PathLike[str]) -> os.stat_result:
    return os.lstat(fs(path))


def exists(path: str | os.PathLike[str]) -> bool:
    return os.path.exists(fs(path))


def lexists(path: str | os.PathLike[str]) -> bool:
    return os.path.lexists(fs(path))


def isdir(path: str | os.PathLike[str]) -> bool:
    return os.path.isdir(fs(path))


def islink(path: str | os.PathLike[str]) -> bool:
    return os.path.islink(fs(path))


def listdir(path: str | os.PathLike[str]) -> list[str]:
    return os.listdir(fs(path))
