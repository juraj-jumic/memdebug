"""A plain folder of markdown files that is not a git repository (for example an agent's own memory folder).

It reuses the markdown adapter's working-tree reader, so links and junctions are never followed, unsafe names and
oversized files are skipped, and the listing is marked incomplete whenever something could not be read. The folder keeps
no history, so memdebug records what it sees between two looks: a change is an observed ADD, UPDATE or DELETE, never an
"outside the history" alarm. Read-only: this adapter never writes.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..errors import AdapterError
from ..models import Memory, MemoryEvent
from ..textsafe import safe_text
from .base import HistoryRead, LiveMemories
from .common import Warnings, clean_id
from .markdown_git import MarkdownGitAdapter, _valid_relpath


class FolderAdapter(MarkdownGitAdapter):
    name = "folder"
    capabilities: set[str] = set()  # no history to read

    def __init__(
        self,
        root: str | Path,
        *,
        store: str | None = None,
        subdir: str | None = None,
        suffixes: tuple[str, ...] = (".md",),
        only: tuple[str, ...] | None = None,
        max_files: int = 20_000,
        max_total_chars: int = 100_000_000,
        clock: Callable[[], datetime] | None = None,
    ):
        try:
            self._root = Path(root).resolve(strict=True)
        except OSError as exc:
            raise AdapterError(f"cannot access the folder: {exc.strerror}") from exc
        if not self._root.is_dir():
            raise AdapterError("the path is not a folder")
        if (self._root / ".git").exists():
            raise AdapterError("this folder is a git repository: use the markdown (git) store type, which also reads its history")
        if not (isinstance(max_files, int) and max_files > 0 and max_total_chars > 0):
            raise AdapterError("max_files and max_total_chars must be positive")
        if not suffixes or any(not s.startswith(".") or s != s.lower() for s in suffixes):
            raise AdapterError("suffixes must be lowercase and start with a dot, such as '.md'")
        self._suffixes = tuple(suffixes)
        self._store = clean_id(store) or clean_id(self._root.name) or "memory"
        self._max_files = max_files
        self._max_total_chars = max_total_chars
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._git = None  # type: ignore[assignment]  # there is no git here; nothing in this class may use it
        self._subdir = self._validate_subdir(subdir)
        self._only: tuple[str, ...] | None = None
        if only is not None:
            names = tuple(only)
            if not names or len(names) > 10 or any("/" in n or _valid_relpath(n, self._suffixes) is None for n in names):
                raise AdapterError("the files to watch must be 1 to 10 plain markdown file names in the folder itself")
            self._only = names

    @property
    def only(self) -> tuple[str, ...] | None:
        """The named files this store is limited to, or None when it covers the whole folder."""
        return self._only

    def read_history(self, max_rows: int) -> HistoryRead:
        return HistoryRead(events=[], refs=set(), truncated=False)

    def history(self, memory_id: str) -> list[MemoryEvent]:
        return []

    def list_memories(self, scope: dict[str, str]) -> LiveMemories:
        """With `only`, exactly those files and nothing else: the rest of the folder (often a settings folder that also holds
        credentials) is never listed or opened."""
        if self._only is None:
            return super().list_memories(scope)
        if scope != {"store": self._store}:
            raise AdapterError(f"scope must be {{'store': {safe_text(self._store, 40)!r}}} for this folder")
        warnings = Warnings()
        memories: list[Memory] = []
        complete = True
        for name in self._only:
            full = os.path.join(self._root, name)
            try:
                os.lstat(full)
            except FileNotFoundError:
                continue  # not created yet (an agent writes it the first time it remembers something)
            except OSError as exc:
                complete = False
                warnings.add(f"{safe_text(name, 60)} could not be inspected: {exc.strerror}")
                continue
            text = self._read_working_file(full, name, warnings)
            if text is None:
                complete = False
                continue
            memories.append(Memory(id=name, text=text, scope={"store": self._store}))
        return LiveMemories(memories=memories, complete=complete, warnings=warnings.as_list())
