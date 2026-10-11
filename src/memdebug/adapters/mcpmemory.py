"""The official MCP "memory" server's file (memory.jsonl), read one entity or relation at a time.

The reference memory server from github.com/modelcontextprotocol/servers keeps a knowledge graph in one JSON Lines file: one object per line,
either an entity (`{"type":"entity","name":...,"entityType":...,"observations":[...]}`) or a relation (`{"type":"relation","from":...,"to":...,
"relationType":...}`). Where the file is depends on the install (the `MEMORY_FILE_PATH` setting, or the server's own folder), so a person points
memdebug at it. The server rewrites the whole file on every change (a temporary file renamed over it), so this adapter opens the path afresh
on each look, never follows a link, and reads nothing but that one file.

The file keeps no history, so memdebug records what it sees between two looks. Everything in it is untrusted text: lines that are not valid or not
of a known shape are skipped with a warning and make the listing incomplete. This adapter only reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .. import longpath
from ..errors import AdapterError
from ..models import MAX_ID_CHARS, Memory, MemoryEvent
from ..textsafe import bound_text, safe_text
from .base import HistoryRead, LiveMemories
from .common import Warnings, require_regular_file

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_RECORDS = 20_000
MAX_OBSERVATIONS = 500


def _record_id(prefix: str, label: str, taken: set[str]) -> str:
    """A memory id from a record's own words, kept within the id limit (the rest is replaced by a hash) and made unique."""
    head = f"{prefix}:{label}"
    if len(head) > MAX_ID_CHARS - 20:
        head = head[:MAX_ID_CHARS - 41] + "~" + hashlib.sha256(label.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    candidate, number = head, 2
    while candidate in taken:  # the server merges duplicates, but a hand-edited file may repeat a name
        candidate, number = f"{head}#{number}", number + 1
    taken.add(candidate)
    return candidate


def _words(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


class McpMemoryAdapter:
    """Read-only adapter for one MCP memory server file. The store's scope is `{"store": <name>}`.

    Raises:
        AdapterError: From the constructor, if the path is not a regular file; from `list_memories`, if the scope is wrong, or the file is
            a link, vanished or too large to read.
    """

    name = "mcpmemory"
    capabilities: set[str] = set()  # no history to read

    def __init__(self, path: str | Path, *, store: str = "mcp"):
        self._path = Path(path)
        self._store = store
        require_regular_file(self._path)

    @property
    def scope(self) -> dict[str, str]:
        """The scope of this store, `{"store": <name>}`."""
        return {"store": self._store}

    def _read(self) -> bytes:
        """The file's bytes: refuses a link or a non-regular file, and a file larger than MAX_FILE_BYTES."""
        full = longpath.fs(self._path)
        try:
            before = os.lstat(full)
            if not stat.S_ISREG(before.st_mode):  # lstat does not follow links, so a link is not "regular" and is refused here
                raise AdapterError("the memory file is a link or not a plain file, so it is not read")
            if before.st_size > MAX_FILE_BYTES:
                raise AdapterError("the memory file is too large to read")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(full, flags)
        except OSError as exc:
            raise AdapterError(f"cannot read the memory file: {exc.strerror}") from exc
        try:
            if not os.path.samestat(before, os.fstat(fd)):
                raise AdapterError("the memory file was replaced while it was being opened")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES:
            raise AdapterError("the memory file is too large to read")
        return data

    def list_memories(self, scope: dict[str, str]) -> LiveMemories:
        """Lists the entities and relations in the file, in file order.

        Each entity is one memory ("entity:<name>", its type and observations as text), each relation another
        ("relation:<from> -[<type>]-> <to>"). A line that is not valid JSON or not a known shape is skipped with a warning and
        makes the listing incomplete, as does a file with more than MAX_RECORDS records.

        Raises:
            AdapterError: If `scope` is not this store's scope, or the file cannot be read.
        """
        if scope != self.scope:
            raise AdapterError(f"scope must be {{'store': {safe_text(self._store, 40)!r}}} for this file")
        warnings = Warnings()
        memories: list[Memory] = []
        taken: set[str] = set()
        complete = True
        text = self._read().decode("utf-8", "replace")
        # split on the newline only, as the server does: str.splitlines() also cuts at U+2028, U+2029 and U+0085, which JSON.stringify writes raw
        for number, line in enumerate(text.split("\n"), 1):
            if not line.strip():
                continue
            if len(memories) >= MAX_RECORDS:
                complete = False
                warnings.add(f"more than {MAX_RECORDS} records; the rest were not read")
                break
            memory = self._record(number, line, taken, warnings)
            if memory is None:
                complete = False
                continue
            memories.append(memory)
        return LiveMemories(memories=memories, complete=complete, warnings=warnings.as_list())

    def _record(self, number: int, line: str, taken: set[str], warnings: Warnings) -> Memory | None:
        if len(line.encode("utf-8", "surrogatepass")) > MAX_LINE_BYTES:
            warnings.add(f"line {number} is too long and was skipped")
            return None
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):  # a line nested thousands of levels deep must cost one line, not the whole store
            warnings.add(f"line {number} is not valid JSON and was skipped")
            return None
        kind = data.get("type") if isinstance(data, dict) else None
        scope = self.scope
        if kind == "entity":
            name, entity_type = _words(data.get("name"), 4000), _words(data.get("entityType"), 4000)
            observations = data.get("observations")
            if name is None or entity_type is None or not isinstance(observations, list) or not all(isinstance(o, str) for o in observations):
                warnings.add(f"line {number} is not a usable entity and was skipped")
                return None
            body = "\n".join(f"- {o}" for o in observations[:MAX_OBSERVATIONS])
            if len(observations) > MAX_OBSERVATIONS:
                body += f"\n... and {len(observations) - MAX_OBSERVATIONS} more observations"
            return Memory(id=_record_id("entity", name, taken), text=bound_text(f"[{entity_type}] {name}\n{body}".rstrip()), scope=scope)
        if kind == "relation":
            source, target, relation = _words(data.get("from"), 4000), _words(data.get("to"), 4000), _words(data.get("relationType"), 4000)
            if source is None or target is None or relation is None:
                warnings.add(f"line {number} is not a usable relation and was skipped")
                return None
            label = f"{source} -[{relation}]-> {target}"
            return Memory(id=_record_id("relation", label, taken), text=bound_text(label), scope=scope)
        warnings.add(f"line {number} is neither an entity nor a relation and was skipped")
        return None

    def read_history(self, max_rows: int) -> HistoryRead:
        """Returns an empty history: the file records none."""
        return HistoryRead(events=[], refs=set(), truncated=False)

    def history(self, memory_id: str) -> list[MemoryEvent]:
        """Returns no events: the file records no history."""
        return []
