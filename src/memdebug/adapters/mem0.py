"""Read-only adapter for self-hosted Mem0 (verified against mem0ai 2.2.1).

Where the data comes from:
  * live memories     -> Mem0's own get_all()  (public API)
  * the global feed   -> Mem0's history database, opened strictly read-only, because the
                         public API cannot list the history of memories that were deleted.
  * one memory's log  -> Mem0's own history()  (public API)

This adapter never writes to Mem0 or to its database. Everything it reads is untrusted.

Mem0 facts this relies on (2.2.1):
  * history rows: id, memory_id, old_memory, new_memory, event, created_at, updated_at,
    is_deleted, actor_id, role. Events are 'ADD', 'UPDATE', 'DELETE'.
  * for UPDATE and DELETE rows, created_at is when the *memory* was created and updated_at is
    when the *event* happened; for ADD both are the creation time.
  * rows have no scope column; scope has to come from the live listing.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from ..errors import AdapterError, MemdebugError, UnsupportedSchemaError
from ..models import MAX_ID_CHARS, Memory, MemoryEvent, Op, Source
from ..textsafe import bound_text, safe_text
from .base import HistoryRead, LiveMemories
from .common import Warnings as _Warnings
from .common import clean_id as _clean_id
from .common import decode as _decode
from .common import parse_ts as _parse_ts
from .common import require_regular_file as _require_regular_file

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("id", "memory_id", "old_memory", "new_memory", "event", "created_at", "updated_at")
OPTIONAL_COLUMNS = ("actor_id", "role")  # fixed names; column names are never taken from data
SCOPE_KEYS = ("user_id", "agent_id", "run_id")
_OPS = {"ADD": Op.ADD, "UPDATE": Op.UPDATE, "DELETE": Op.DELETE}
_MAX_CONFIG_BYTES = 1_000_000


def _text_or_none(value: object) -> tuple[bool, str | None]:
    if value is None:
        return True, None
    if isinstance(value, str):
        return True, bound_text(value)
    return False, None  # BLOB, number or anything else SQLite's loose typing allowed in


def validate_scope(scope: object) -> dict[str, str]:
    """Checks a Mem0 scope and returns a copy of it.

    Raises:
        AdapterError: If `scope` is not a non-empty dict, has a key other than user_id, agent_id or run_id, or has a
            value that is not a non-empty string of at most MAX_ID_CHARS characters.
    """
    if not isinstance(scope, dict) or not scope:
        raise AdapterError("a scope is required: give user_id, agent_id or run_id")
    clean: dict[str, str] = {}
    for key, value in scope.items():
        if key not in SCOPE_KEYS:
            raise AdapterError(f"unsupported scope key: {safe_text(key, 30)}")
        if _clean_id(value) is None:
            raise AdapterError(f"scope value for {key} must be a non-empty string of at most {MAX_ID_CHARS} characters")
        clean[key] = value
    return clean


class Mem0Adapter:
    """Read-only adapter for a self-hosted Mem0 (see the module docstring for what is read from where).

    Args:
        memory: A mem0 `Memory` object; it must have callable `get_all` and `history` methods. Used for the live listing
            and for one memory's history, so those two calls are made by Mem0's own code, not by this adapter.
        history_db_path: Mem0's history database file. It is opened read-only, and only the `history` table is read.
        max_rows: The most rows `history()` takes from Mem0's per-memory history.
        live_limit: The most memories requested from Mem0 for one listing. A listing that reaches it is marked incomplete.
        max_total_chars: The text budget for `read_history`.
        clock: Supplies "now", for tests.

    Raises:
        AdapterError: From the constructor, if `memory` lacks `get_all` or `history`, a limit is not positive, or the
            history file is not a readable regular file.
        UnsupportedSchemaError: From the constructor, if the file has no `history` table or lacks expected columns.
    """

    name = "mem0"
    capabilities = {"history", "global_feed"}

    def __init__(
        self,
        memory: Any,
        history_db_path: str | Path,
        *,
        max_rows: int = 100_000,
        live_limit: int = 10_000,
        max_total_chars: int = 100_000_000,
        clock: Callable[[], datetime] | None = None,
    ):
        for attribute in ("get_all", "history"):
            if not callable(getattr(memory, attribute, None)):
                raise AdapterError(f"memory object has no callable {attribute}(); expected a mem0 Memory")
        if not (isinstance(max_rows, int) and max_rows > 0 and isinstance(live_limit, int) and live_limit > 0):
            raise AdapterError("max_rows and live_limit must be positive integers")
        self._memory = memory
        self._path = Path(history_db_path)
        self._max_rows = max_rows
        self._live_limit = live_limit
        self._max_total_chars = max_total_chars
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with closing(self._connect_ro()) as conn:  # fail early on a wrong file
            self._check_schema(conn)

    # -- read-only database access ----------------------------------------------------

    def _connect_ro(self) -> sqlite3.Connection:
        path = _require_regular_file(self._path)
        try:
            # mode=ro: never creates the file, never writes. query_only is a second lock.
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
            conn.text_factory = _decode
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.Error as exc:
            raise AdapterError(f"cannot open history file read-only: {exc}") from exc

    @staticmethod
    def _check_schema(conn: sqlite3.Connection) -> set[str]:
        try:
            present = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
        except sqlite3.Error as exc:
            raise AdapterError(f"cannot read history file: {exc}") from exc
        if not present:
            raise UnsupportedSchemaError("no 'history' table found; is this Mem0's history database?")
        missing = [c for c in REQUIRED_COLUMNS if c not in present]
        if missing:
            raise UnsupportedSchemaError(f"history table lacks expected columns: {', '.join(missing)}")
        return present

    # -- row -> event -------------------------------------------------------------------

    def _event_from_row(
        self, row: dict[str, Any], label: str, ref_fallback: str, now: datetime, warnings: _Warnings
    ) -> tuple[MemoryEvent | None, str]:
        ref = _clean_id(row.get("id")) or ref_fallback
        memory_id = _clean_id(row.get("memory_id"))
        if memory_id is None:
            warnings.add(f"{label}: missing or invalid memory_id; row skipped")
            return None, ref
        raw_event = row.get("event")
        op = _OPS.get(raw_event.strip().upper()) if isinstance(raw_event, str) else None
        if op is None:
            warnings.add(f"{label}: unknown event {safe_text(raw_event, 30)!r}; row skipped")
            return None, ref
        ok_old, old = _text_or_none(row.get("old_memory"))
        ok_new, new = _text_or_none(row.get("new_memory"))
        if not (ok_old and ok_new):
            warnings.add(f"{label}: memory text is not text; row skipped")
            return None, ref
        if op is Op.UPDATE and new is None:
            warnings.add(f"{label}: UPDATE without new text")

        created = _parse_ts(row.get("created_at"))
        updated = _parse_ts(row.get("updated_at"))
        ts = (created or updated) if op is Op.ADD else (updated or created)
        observed = False
        if ts is None:
            ts, observed = now, True
            warnings.add(f"{label}: no usable timestamp; using the time it was read")
        elif ts > now + timedelta(days=1):
            warnings.add(f"{label}: timestamp is in the future")

        actor = _clean_id(row.get("actor_id"))
        role = _clean_id(row.get("role"))
        source = Source(actor_id=actor, role=role) if (actor or role) else None
        try:
            event = MemoryEvent(
                backend=self.name, memory_id=memory_id, op=op, ts=ts, ts_observed=observed,
                backend_ref=ref, before=old, after=new, source=source,
            )
        except ValidationError:
            warnings.add(f"{label}: row failed validation; skipped")
            return None, ref
        return event, ref

    # -- the adapter interface -------------------------------------------------------------

    def read_history(self, max_rows: int) -> HistoryRead:
        """Reads the global history table directly, including rows for memories that were since deleted.

        Opens Mem0's history database read-only and takes the first `max_rows` rows in row order; the events are then
        sorted by time. Rows that cannot be understood are counted in `skipped` and reported as warnings. The result
        is marked truncated when there are more rows or the text budget (`max_total_chars`) was exceeded.

        Raises:
            AdapterError: If `max_rows` is not positive or the database cannot be read.
            UnsupportedSchemaError: If the history table lacks expected columns.
        """
        if not (isinstance(max_rows, int) and max_rows > 0):
            raise AdapterError("max_rows must be a positive integer")
        warnings = _Warnings()
        now = self._clock()
        pairs: list[tuple[MemoryEvent, int]] = []
        refs: set[str] = set()
        skipped = 0
        truncated = False
        total_chars = 0
        with closing(self._connect_ro()) as conn:
            present = self._check_schema(conn)
            names = list(REQUIRED_COLUMNS) + [c for c in OPTIONAL_COLUMNS if c in present]
            # Identifiers come from the fixed tuples above; values are always bound parameters.
            query = f"SELECT rowid, {', '.join(names)} FROM history ORDER BY rowid LIMIT ?"
            try:
                for index, db_row in enumerate(conn.execute(query, (max_rows + 1,))):
                    if index >= max_rows:
                        truncated = True
                        break
                    rowid = db_row[0]
                    row = dict(zip(names, db_row[1:], strict=False))
                    event, ref = self._event_from_row(row, f"history rowid {rowid}", f"rowid:{rowid}", now, warnings)
                    refs.add(ref)
                    if event is None:
                        skipped += 1
                        continue
                    total_chars += len(event.before or "") + len(event.after or "")
                    if total_chars > self._max_total_chars:
                        truncated = True
                        warnings.add("history text is larger than the memory budget; the rest was not read")
                        break
                    pairs.append((event, rowid))
            except sqlite3.Error as exc:
                raise AdapterError(f"reading history failed: {exc}") from exc
        pairs.sort(key=lambda pair: (pair[0].ts, pair[1]))
        if truncated:
            warnings.add(f"history has more than {max_rows} rows; only the oldest {max_rows} were read")
        return HistoryRead(
            events=[event for event, _ in pairs], refs=refs, truncated=truncated,
            skipped=skipped, warnings=warnings.as_list(),
        )

    def list_memories(self, scope: dict[str, str]) -> LiveMemories:
        """Lists live memories through Mem0's own `get_all`, including expired ones.

        The result is marked incomplete when the count reaches `live_limit` or an item is skipped (missing id or text,
        or failed validation); a duplicate id is skipped without that. Each memory's scope is the requested scope
        plus any scope keys Mem0 reports on the item.

        Raises:
            AdapterError: If `scope` is invalid (see `validate_scope`), `get_all` fails, or it returns an unexpected
                shape.
        """
        clean = validate_scope(scope)
        try:
            result = self._memory.get_all(filters=dict(clean), top_k=self._live_limit, show_expired=True)
        except Exception as exc:  # any failure inside the backend becomes one controlled error
            raise AdapterError(f"mem0 get_all failed: {type(exc).__name__}: {safe_text(exc, 200)}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise AdapterError("mem0 get_all returned an unexpected shape; is this mem0ai 2.x?")
        items = result["results"]
        warnings = _Warnings()
        complete = len(items) < self._live_limit
        if not complete:
            warnings.add(
                f"listing reached the limit of {self._live_limit}; absence will not be reported as deletion"
            )
        memories: list[Memory] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            memory_id = _clean_id(item.get("id")) if isinstance(item, dict) else None
            text = item.get("memory") if isinstance(item, dict) else None
            if memory_id is None or not isinstance(text, str):
                warnings.add(f"live item {index}: missing id or text; skipped")
                complete = False  # a hidden memory could be mistaken for a deleted one
                continue
            if memory_id in seen:
                warnings.add(f"live item {index}: duplicate id; skipped")
                continue
            seen.add(memory_id)
            item_scope = {k: item[k] for k in SCOPE_KEYS if _clean_id(item.get(k))}
            try:
                memories.append(Memory(id=memory_id, text=bound_text(text), scope={**clean, **item_scope}))
            except ValidationError:
                warnings.add(f"live item {index}: failed validation; skipped")
                complete = False
        return LiveMemories(memories=memories, complete=complete, warnings=warnings.as_list())

    def history(self, memory_id: str) -> list[MemoryEvent]:
        """Returns one memory's events through Mem0's own `history()`, oldest first.

        Reads at most `max_rows` rows. Rows that cannot be understood are skipped, and their warnings are logged
        rather than returned.

        Raises:
            AdapterError: If `memory_id` is not usable, or `history()` fails or returns an unexpected shape.
        """
        if _clean_id(memory_id) is None:
            raise AdapterError(f"memory_id must be a non-empty string of at most {MAX_ID_CHARS} characters")
        try:
            rows = self._memory.history(memory_id)
        except Exception as exc:
            raise AdapterError(f"mem0 history failed: {type(exc).__name__}: {safe_text(exc, 200)}") from exc
        if not isinstance(rows, list):
            raise AdapterError("mem0 history returned an unexpected shape")
        warnings = _Warnings()
        now = self._clock()
        pairs: list[tuple[MemoryEvent, int]] = []
        for index, row in enumerate(rows[: self._max_rows]):
            if not isinstance(row, dict):
                warnings.add(f"history item {index}: not a record; skipped")
                continue
            event, _ = self._event_from_row(row, f"history item {index}", f"api:{index}", now, warnings)
            if event is not None:
                pairs.append((event, index))
        for message in warnings.as_list():
            logger.warning("%s", message)
        pairs.sort(key=lambda pair: (pair[0].ts, pair[1]))
        return [event for event, _ in pairs]


# -- starting mem0 -------------------------------------------------------------------------


def ensure_mem0_telemetry_off() -> list[str]:
    """Mem0 sends anonymous usage events by default. Turn that off before it is imported."""
    os.environ.setdefault("MEM0_TELEMETRY", "false")
    notes: list[str] = []
    if os.environ["MEM0_TELEMETRY"].strip().lower() in ("true", "1", "yes"):
        notes.append("MEM0_TELEMETRY is on in your environment; mem0 will send anonymous usage reports")
    else:
        module = sys.modules.get("mem0.memory.telemetry")
        if module is not None and getattr(module, "MEM0_TELEMETRY", False):
            notes.append(
                "mem0 was already imported with telemetry on; start a fresh process with "
                "MEM0_TELEMETRY=false to stop its anonymous usage reports"
            )
    return notes


def _load_json_config(path: Path) -> dict[str, Any]:
    resolved = _require_regular_file(path)
    if resolved.stat().st_size > _MAX_CONFIG_BYTES:
        raise AdapterError("mem0 config file is too large")
    try:
        config = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdapterError("mem0 config file is not valid JSON") from exc
    if not isinstance(config, dict):
        raise AdapterError("mem0 config must be a JSON object")
    return config


def build_mem0_memory(config_path: str | Path | None = None) -> tuple[Any, list[str]]:
    """Create a mem0 Memory. Errors never echo the config, which may hold API keys."""
    notes = ensure_mem0_telemetry_off()
    try:
        from mem0 import Memory
    except ImportError as exc:
        raise AdapterError("mem0ai is not installed; run: pip install 'mem0ai>=2'") from exc
    try:
        if config_path is None:
            return Memory(), notes
        return Memory.from_config(_load_json_config(Path(config_path))), notes
    except MemdebugError:
        raise
    except Exception as exc:
        raise AdapterError(f"could not start mem0: {type(exc).__name__}") from exc
