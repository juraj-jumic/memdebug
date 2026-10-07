"""Open WebUI's memory feature, read from a COPY of its database (webui.db).

Open WebUI keeps what it remembers about a user in a `memory` table and records no change history, so memdebug records
what it sees between two looks. This adapter only reads: the file is opened read-only (SQLite's read-only mode plus
`query_only`), only the `memory` and `user` tables are touched, and every row is validated. It never reads chats,
passwords, API keys or settings. Work on a copy of the database, not the live file inside the container.
"""
from __future__ import annotations

import bisect
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..errors import AdapterError
from ..models import Memory, MemoryEvent, Source, SourceKind
from ..textsafe import bound_text, safe_text
from .base import HistoryRead, LiveMemories
from .common import Warnings, clean_id, decode, require_regular_file

MAX_ID_CHARS = 256
_REQUIRED = {"id", "user_id", "content"}
_PLAIN = re.compile(r"^[A-Za-z0-9_.:/ -]{1,40}\Z")
WINDOW_SECONDS = 120          # a chat message this close to a memory's last change counts as "around the same time"
MAX_CHAT_ROWS = 200_000       # chat records compared with; more than this and the comparison is skipped
_OPTIONAL = ("type", "meta", "created_at", "updated_at")


def _plain(value: object) -> str | None:
    """A short label made of plain characters, or just its size if it is anything else. Never free text."""
    if value is None or value == "":
        return None
    text = str(value)
    return text if _PLAIN.match(text) else f"(text, {len(text)} chars)"


def _epoch(value: object) -> float | None:
    """Seconds since 1970, from seconds or milliseconds; anything else is not trusted as a time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    for low, high, divisor in ((1e9, 1e11, 1), (1e12, 1e14, 1000)):
        if low <= value < high:
            return value / divisor
    return None


class OpenWebUIAdapter:
    name = "openwebui"
    capabilities: set[str] = set()  # no history to read

    def __init__(self, db_path: str | Path, *, user_id: str | None = None, max_rows: int = 50_000,
                 clock: Callable[[], datetime] | None = None):
        self._path = Path(db_path)
        self._max_rows = max_rows
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not (isinstance(max_rows, int) and max_rows > 0):
            raise AdapterError("max_rows must be positive")
        wanted = None if user_id is None else clean_id(user_id)
        if user_id is not None and wanted is None:
            raise AdapterError("the user id must be a non-empty string of reasonable length")
        with closing(self._connect()) as db:
            columns = self._columns(db, "memory")
            if not _REQUIRED <= columns:
                raise AdapterError("this does not look like Open WebUI's database: the memory table lacks the expected columns")
            self._user = wanted or self._only_user(db)

    # -- reading ---------------------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        path = require_regular_file(self._path)
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
            conn.text_factory = decode
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.Error as exc:
            raise AdapterError(f"cannot open the Open WebUI database read-only: {safe_text(exc, 100)}") from exc

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
        except sqlite3.Error as exc:
            raise AdapterError(f"cannot read the database structure: {safe_text(exc, 100)}") from exc

    def _only_user(self, db: sqlite3.Connection) -> str:
        """The user whose memories to read, when none was named: the single user of this installation."""
        try:
            owners = {clean_id(row[0]) for row in db.execute('SELECT DISTINCT "user_id" FROM "memory" LIMIT 3')}
            owners.discard(None)
            if len(owners) == 1:
                return str(next(iter(owners)))
            if len(owners) > 1:
                raise AdapterError("memories of several users are stored here; say which one with --user-id")
            if "user" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} and "id" in self._columns(db, "user"):
                users = {clean_id(row[0]) for row in db.execute('SELECT "id" FROM "user" LIMIT 3')}
                users.discard(None)
                if len(users) == 1:
                    return str(next(iter(users)))
        except sqlite3.Error as exc:
            raise AdapterError(f"cannot read the Open WebUI database: {safe_text(exc, 100)}") from exc
        raise AdapterError("cannot tell whose memories to read; say which user with --user-id")

    @property
    def user(self) -> str:
        return self._user

    @property
    def scope(self) -> dict[str, str]:
        return {"user_id": self._user}

    def list_memories(self, scope: dict[str, str]) -> LiveMemories:
        if scope != self.scope:
            raise AdapterError(f"scope must be {{'user_id': {safe_text(self._user, 40)!r}}} for this database")
        warnings = Warnings()
        memories: list[Memory] = []
        complete = True
        with closing(self._connect()) as db:
            have = self._columns(db, "memory")
            extra = [name for name in _OPTIONAL if name in have]
            order = '"created_at", "id"' if "created_at" in have else '"id"'
            try:
                rows = db.execute(f'SELECT "id", "content"{"".join(f", {chr(34)}{n}{chr(34)}" for n in extra)} FROM "memory" '
                                  f'WHERE "user_id" = ? ORDER BY {order} LIMIT ?', (self._user, self._max_rows + 1)).fetchall()
            except sqlite3.Error as exc:
                raise AdapterError(f"cannot read the memories: {safe_text(exc, 100)}") from exc
            chat = self._chat_times(db)
        if len(rows) > self._max_rows:
            rows = rows[:self._max_rows]
            complete = False
            warnings.add(f"more than {self._max_rows} memories; the rest were not read")
        for raw_id, content, *details in rows:
            memory_id = clean_id(raw_id)
            if memory_id is None or not isinstance(content, str):
                complete = False
                warnings.add("a memory row without a usable id or text was skipped")
                continue
            facts = dict(zip(extra, details, strict=True))
            memories.append(Memory(id=memory_id, text=bound_text(content), scope=dict(scope), source=self._source(facts, chat)))
        return LiveMemories(memories=memories, complete=complete, warnings=warnings.as_list())

    @staticmethod
    def _chat_times(db: sqlite3.Connection) -> list[tuple[float, str]] | None:
        """When each chat message happened and its role, nothing else, or None if that cannot be compared. The message text is never read."""
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "chat_message" not in tables or not {"role", "created_at"} <= OpenWebUIAdapter._columns(db, "chat_message"):
                return None
            rows = db.execute('SELECT "created_at", "role" FROM "chat_message" WHERE "created_at" IS NOT NULL '
                              'ORDER BY "created_at" LIMIT ?', (MAX_CHAT_ROWS + 1,)).fetchall()
        except sqlite3.Error:
            return None
        if len(rows) > MAX_CHAT_ROWS:
            return None
        times = [(t, _plain(role) or "unknown") for raw, role in rows if (t := _epoch(raw)) is not None]
        return sorted(times)

    @staticmethod
    def _source(facts: dict, chat: list[tuple[float, str]] | None) -> Source | None:
        """What Open WebUI itself records about a memory, and how close a chat was to it. A description, never a verdict."""
        meta = facts.get("meta")
        data = None
        if isinstance(meta, str) and len(meta) <= 20_000:
            try:
                data = json.loads(meta)
            except ValueError:
                data = None
        label = _plain(data.get("created_by")) if isinstance(data, dict) else None
        kind = _plain(facts.get("type"))
        touched = _epoch(facts.get("updated_at")) or _epoch(facts.get("created_at"))
        if label is None and kind is None and touched is None:
            return None
        parts: list[str] = []
        near: list[tuple[float, str]] = []
        if touched is not None:
            parts.append("last touched " + datetime.fromtimestamp(touched, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            if chat is None:
                parts.append("chat records could not be compared")
            elif not chat:
                parts.append("there are no chat messages to compare with")
            else:
                low = bisect.bisect_left(chat, (touched - WINDOW_SECONDS, ""))
                high = bisect.bisect_right(chat, (touched + WINDOW_SECONDS, "\uffff"))
                near = chat[low:high]
                position = bisect.bisect_left(chat, (touched, ""))
                gap = min(abs(chat[i][0] - touched) for i in (position - 1, position) if 0 <= i < len(chat))
                if near:
                    parts.append(f"{len(near)} chat message{'' if len(near) == 1 else 's'} within {WINDOW_SECONDS} s (nearest {gap:.0f} s away)")
                else:
                    parts.append(f"no chat message within {WINDOW_SECONDS} s (nearest is {gap:.0f} s away)")
        if label == "manual" and not near and touched is not None and chat:
            parts.append("consistent with being added by hand in the settings")
        elif label == "manual" and near:
            parts.append("labelled manual, but a chat was active at the time")
        elif label != "manual" and near:
            parts.append(f"{'not labelled manual' if label is None else 'labelled ' + label}, and a chat was active at the time: "
                         "check what the assistant had just read")
        return Source(kind=SourceKind.UNKNOWN, actor_id=f"created_by={label}" if label else None,
                      role=f"type={kind}" if kind else None, note="; ".join(parts)[:390] or None)

    def read_history(self, max_rows: int) -> HistoryRead:
        return HistoryRead(events=[], refs=set(), truncated=False)

    def history(self, memory_id: str) -> list[MemoryEvent]:
        return []
