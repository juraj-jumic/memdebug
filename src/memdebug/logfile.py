"""A small log file for `memdebug watch --log`, so what a background watcher finds is not lost when there is no terminal to print it to.

Every line is cleaned like everything else memdebug shows (a memory's words are untrusted), the file is private to the user where the system
allows it, and it never grows without bound: past a size limit it is renamed to `<name>.1` (replacing the previous one) and a new file starts.
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

    def __call__(self, line: str) -> None:
        """Write one line (cleaned and shortened). Returns nothing; errors are swallowed."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                info = os.lstat(self.path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    return  # never write through a link or into something that is not a plain file
                if info.st_size > self._max:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(safe_text(line, MAX_LINE) + "\n")
        except OSError:
            return
