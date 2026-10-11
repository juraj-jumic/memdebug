"""A small log file for `memdebug watch --log`, so what a background watcher finds is not lost when there is no terminal to print it to.

Every line is cleaned like everything else memdebug shows (a memory's words are untrusted), the file is private to the user where the system
allows it, and it never grows without bound: past a size limit it is renamed to `<name>.1` (replacing the previous one) and a new file starts.
If that rename is impossible (on Windows, another program may have the log open) the line is still written, so logging never goes silent.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .textsafe import safe_text

MAX_BYTES = 1_000_000
MAX_LINE = 800


class LogFile:
    """Appends lines to a file, keeping it small. Failing to write is never fatal: the watching goes on.

    Args:
        path: The log file. It is created when the first line is written; the folder must exist or be creatable.
        max_bytes: The size after which the file is renamed to `<name>.1` and a new one started.
    """

    def __init__(self, path: Path, max_bytes: int = MAX_BYTES):
        self.path = Path(path)
        self._max = max_bytes

    def _rotate_if_needed(self) -> bool:
        """Rename a file that has grown too large. Returns False if the path holds something that is not a plain file (then nothing is written)."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return True
        if not stat.S_ISREG(info.st_mode):  # a link or a folder is never written into
            return False
        if info.st_size > self._max:
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            except OSError:
                pass  # for example the file is open in another program: keep appending to it instead of going silent
        return True

    def __call__(self, line: str) -> None:
        """Write one line (cleaned and shortened). Returns nothing; errors are swallowed."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self._rotate_if_needed():
                return
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)  # never follow a link that appeared since the check
            fd = os.open(self.path, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    handle.write(safe_text(line, MAX_LINE) + "\n")
        except OSError:
            return
