"""Append-only, hash-chained event ledger stored in SQLite.

What the chain protects: edits to, or removal from the middle of, the ledger. Removing the
newest entries cannot be seen from the chain alone, so verify() accepts the head hash that a
snapshot recorded.

What it does not protect: someone who can rewrite the whole file can rebuild a valid chain.
Anchor the head hash somewhere else (a snapshot, a printed line) if that matters.

The file holds memory text, including text of deleted memories, so it is created with
owner-only permissions and by design cannot be edited or erased entry by entry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from pydantic import ValidationError

from .errors import LedgerConflictError, LedgerError, SnapshotError
from .models import (
    META_OPS,
    LedgerEntry,
    Memory,
    MemoryEvent,
    Op,
    Snapshot,
    SnapshotInfo,
    Trust,
    derive_trust,
)
from .textsafe import has_unsafe_chars

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
SCHEMA_VERSION = 3

MAX_SNAPSHOT_MEMORIES = 100_000
MAX_SNAPSHOT_CHARS = 100_000_000
MAX_LABEL_CHARS = 100
_SNAPSHOT_ID_RE = re.compile(r"^s([1-9][0-9]{0,8})\Z")
_EVENT_ID_RE = re.compile(r"^e([1-9][0-9]{0,11})\Z")
_IN_CHUNK = 500  # bound parameters per query (SQLite allows at least 999)
MAX_ROLLBACK_FILES = 200      # files named in a rollback record; the count of all of them is kept as well
_PATH_FORBIDDEN = frozenset('"\\:*?<>|')  # the same names the markdown adapter refuses to read or write
_ROLLBACK_ACTIONS = ("restore", "recreate", "remove")
_ROLLBACK_SOURCES = ("git", "snapshot", "none")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BACKUP_REF_RE = re.compile(r"^refs/memdebug/backups/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")


def canonical(event: MemoryEvent) -> str:
    return json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def compute_hash(prev_hash: str, event_id: str, payload: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{event_id}|{payload}".encode()).hexdigest()


def parse_snapshot_id(value: object) -> int:
    match = _SNAPSHOT_ID_RE.match(value) if isinstance(value, str) else None
    if not match:
        raise SnapshotError("a snapshot id looks like s1, s2, s3 ...")
    return int(match.group(1))


def _text_digest(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise SnapshotError("a memory contains text that is not valid Unicode") from exc


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def snapshot_digest(
    number: int, backend: str, scope: dict, taken_at: str, ledger_seq: int, ledger_head: str,
    complete: bool, label: str | None, entries: list[list],
) -> str:
    """Hash of everything that defines a snapshot. The ledger chain stores this value, so a
    snapshot cannot be altered without the chain noticing.
    """
    body = {
        "id": number, "backend": backend, "scope": scope, "taken_at": taken_at,
        "ledger_seq": ledger_seq, "ledger_head": ledger_head, "complete": bool(complete),
        "label": label, "entries": sorted(entries, key=lambda e: e[0]),
    }
    return hashlib.sha256(_canonical_json(body).encode()).hexdigest()


def validate_rollback_details(value: object) -> dict:
    """The facts a rollback record may hold, checked strictly: it is written by us, but read back from a file that
    anyone could edit, so everything is verified again when the ledger is verified.
    """
    if not isinstance(value, dict) or set(value) != {"target", "before_snapshot", "after_snapshot", "commit",
                                                     "previous_head", "backup", "file_count", "files"}:
        raise ValueError("unexpected rollback fields")
    for key in ("target", "before_snapshot", "after_snapshot"):
        if not (value[key] is None and key != "target") and not (isinstance(value[key], str) and _SNAPSHOT_ID_RE.match(value[key])):
            raise ValueError(f"{key} is not a snapshot id")
    for key in ("commit", "previous_head"):
        if value[key] is not None and not (isinstance(value[key], str) and _SHA_RE.match(value[key])):
            raise ValueError(f"{key} is not a commit id")
    if value["backup"] is not None and not (isinstance(value["backup"], str) and _BACKUP_REF_RE.match(value["backup"])):
        raise ValueError("backup is not a memdebug backup reference")
    files = value["files"]
    if not (isinstance(value["file_count"], int) and not isinstance(value["file_count"], bool) and value["file_count"] >= 0):
        raise ValueError("file_count is not a count")
    if not isinstance(files, list) or len(files) > MAX_ROLLBACK_FILES or len(files) > value["file_count"]:
        raise ValueError("files is not a short list")
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "action", "source"}
                or not isinstance(item["path"], str) or not 0 < len(item["path"]) <= 256 or has_unsafe_chars(item["path"])
                or any(ch in _PATH_FORBIDDEN for ch in item["path"]) or item["path"].startswith("/")
                or ".." in item["path"].split("/") or item["action"] not in _ROLLBACK_ACTIONS
                or item["source"] not in _ROLLBACK_SOURCES):
            raise ValueError("a file entry is malformed")
    return value


def _snap_key(event: MemoryEvent) -> str | None:
    """Index key for snapshot bookkeeping events, kept apart from backend row ids so that no
    backend can ever collide with it.
    """
    if event.op not in META_OPS:
        return None
    return f"{event.op.value}:{event.memory_id.removeprefix('snapshot:')}"


def _decode(raw: bytes) -> str:
    # Invalid UTF-8 in a stored value must not crash a read.
    return raw.decode("utf-8", "replace")


def _prepare_file(path: Path) -> None:
    """Create the file with 0600, or check the existing one. Refuses symlinks and non-files."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise LedgerError(f"directory does not exist: {path.parent}") from None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            os.close(os.open(path, flags, 0o600))
        except FileExistsError:
            pass
        except OSError as exc:
            raise LedgerError(f"cannot create ledger file: {exc.strerror}") from exc
        return
    except OSError as exc:
        raise LedgerError(f"cannot access ledger file: {exc.strerror}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise LedgerError("ledger path is a symlink; refusing to follow it")
    if not stat.S_ISREG(info.st_mode):
        raise LedgerError("ledger path is not a regular file")
    if os.name == "posix" and info.st_mode & 0o077:
        logger.warning(
            "ledger file is readable by other users (mode %o); it holds memory text. "
            "Run: chmod 600 on it.",
            stat.S_IMODE(info.st_mode),
        )


@dataclass
class VerifyResult:
    ok: bool = True
    problems: list[str] = field(default_factory=list)


class Ledger:
    def __init__(self, path: str | Path, *, readonly: bool = False, busy_timeout: float = 5.0):
        self.path = Path(path)
        self._readonly = readonly
        self._busy_timeout = busy_timeout
        if readonly:
            self._open_readonly()
            return
        _prepare_file(self.path)
        try:
            self._db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            self._db.text_factory = _decode
            self._init_schema()
        except sqlite3.Error as exc:
            raise LedgerError(f"cannot open ledger: {exc}") from exc

    @classmethod
    def open_readonly(cls, path: str | Path, *, busy_timeout: float = 5.0) -> "Ledger":
        """Open an existing ledger so that nothing can be written: the file is opened with SQLite's
        read-only mode, never created and never upgraded. Used by the viewer.
        """
        return cls(path, readonly=True, busy_timeout=busy_timeout)

    def _open_readonly(self) -> None:
        try:
            info = os.lstat(self.path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise LedgerError("the ledger path is not a plain regular file")
            uri = self.path.resolve(strict=True).as_uri() + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, timeout=self._busy_timeout, isolation_level=None)
            self._db.text_factory = _decode
            self._db.execute("PRAGMA query_only = ON")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except OSError as exc:
            raise LedgerError(f"cannot access the ledger file: {exc.strerror}") from exc
        except sqlite3.Error as exc:
            raise LedgerError(f"cannot open the ledger read-only: {exc}") from exc
        if "events" not in tables or version not in (1, 2, SCHEMA_VERSION):
            self._db.close()
            raise LedgerError("this file is not a memdebug ledger of a supported version")
        if version != SCHEMA_VERSION:
            self._db.close()
            raise LedgerError(
                "this ledger is from an older version; run any normal memdebug command once "
                "(for example 'memdebug verify') to upgrade it, then try again"
            )

    def _require_writable(self) -> None:
        if self._readonly:
            raise LedgerError("this ledger was opened read-only")

    def close(self) -> None:
        self._db.close()

    # -- internals -------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            self._db.execute("COMMIT")

    def _init_schema(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 0 and not tables:
            with self._tx():
                self._create_events_table()
                self._create_snapshot_tables()
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version in (1, 2) and "events" in tables:
            with self._tx():  # an older ledger is upgraded in place; its events and chain are untouched
                if version == 1:
                    self._db.execute("ALTER TABLE events ADD COLUMN snap TEXT")
                    self._db.execute(
                        "CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL"
                    )
                    self._create_snapshot_tables()
                self._db.execute("ALTER TABLE events ADD COLUMN op TEXT")
                self._db.execute("ALTER TABLE events ADD COLUMN trust TEXT")
                for seq, payload in self._db.execute("SELECT seq, payload FROM events").fetchall():
                    try:
                        data = json.loads(payload)
                        values = (data.get("op"), data.get("trust"), seq)
                    except (ValueError, AttributeError):
                        continue  # left empty; verify reports it
                    self._db.execute("UPDATE events SET op = ?, trust = ? WHERE seq = ?", values)
                self._create_filter_indexes()
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION or "events" not in tables:
            raise LedgerError("this file is not a memdebug ledger of a supported version")

    def _create_events_table(self) -> None:
        self._db.execute(
            "CREATE TABLE events ("
            "seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, backend TEXT NOT NULL, "
            "ref TEXT, snap TEXT, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, "
            "op TEXT, trust TEXT)"
        )
        self._db.execute("CREATE UNIQUE INDEX idx_events_ref ON events(backend, ref) WHERE ref IS NOT NULL")
        self._db.execute("CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL")
        self._create_filter_indexes()

    def _create_filter_indexes(self) -> None:
        self._db.execute("CREATE INDEX idx_events_op ON events(op, seq)")
        self._db.execute("CREATE INDEX idx_events_trust ON events(trust, seq)")

    def _create_snapshot_tables(self) -> None:
        self._db.execute("CREATE TABLE blobs (hash TEXT PRIMARY KEY, text TEXT NOT NULL)")
        self._db.execute(
            "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL, "
            "scope TEXT NOT NULL, taken_at TEXT NOT NULL, ledger_seq INTEGER NOT NULL, "
            "ledger_head TEXT NOT NULL, complete INTEGER NOT NULL, label TEXT, count INTEGER NOT NULL, "
            "entries_hash TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL, memory_id TEXT NOT NULL, "
            "hash TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY (snapshot_id, memory_id))"
        )

    # -- writing ---------------------------------------------------------------------

    def _append_locked(self, events: Sequence[MemoryEvent]) -> list[LedgerEntry]:
        """Append inside a transaction the caller already holds."""
        entries: list[LedgerEntry] = []
        row = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq = row[0] if row else 0
        prev = row[1] if row else GENESIS
        for event in events:
            if event.trust == Trust.UNKNOWN and event.source is not None:
                event = event.model_copy(update={"trust": derive_trust(event.source)})
            seq += 1
            event_id = f"e{seq}"
            payload = canonical(event)
            digest = compute_hash(prev, event_id, payload)
            snap = _snap_key(event)
            self._db.execute(
                "INSERT INTO events (seq, id, backend, ref, snap, payload, prev_hash, hash, op, trust) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, event_id, event.backend, event.backend_ref, snap, payload, prev, digest,
                 event.op.value, event.trust.value),
            )
            entries.append(LedgerEntry(seq=seq, id=event_id, event=event, prev_hash=prev, hash=digest))
            prev = digest
        return entries

    def append_many(self, events: Sequence[MemoryEvent]) -> list[LedgerEntry]:
        """Append events in one transaction: all of them, or none."""
        self._require_writable()
        if not events:
            return []
        if any(e.op in META_OPS for e in events):
            raise LedgerError("snapshot bookkeeping events can only be written by the snapshot methods")
        try:
            with self._tx():
                return self._append_locked(events)
        except sqlite3.IntegrityError as exc:
            raise LedgerConflictError("write collided with another writer; nothing was written") from exc
        except sqlite3.Error as exc:
            raise LedgerError(f"ledger write failed: {exc}") from exc

    def append(self, event: MemoryEvent) -> LedgerEntry:
        return self.append_many([event])[0]

    # -- reading ---------------------------------------------------------------------

    def head(self) -> str:
        row = self._db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def backends(self) -> set[str]:
        """The store types that have events on record."""
        return {row[0] for row in self._db.execute("SELECT DISTINCT backend FROM events") if row[0]}

    def has_events(self, backend: str) -> bool:
        row = self._db.execute("SELECT 1 FROM events WHERE backend = ? LIMIT 1", (backend,)).fetchone()
        return row is not None

    def known_refs(self, backend: str) -> set[str]:
        rows = self._db.execute(
            "SELECT ref FROM events WHERE backend = ? AND ref IS NOT NULL", (backend,)
        ).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def _entry_from_row(seq, event_id, payload, prev, digest) -> LedgerEntry:
        try:
            event = MemoryEvent.model_validate_json(payload)
        except ValidationError as exc:
            raise LedgerError(
                f"entry seq {seq} is unreadable; run 'memdebug verify' to check the ledger"
            ) from exc
        return LedgerEntry(seq=seq, id=event_id, event=event, prev_hash=prev, hash=digest)

    def entries(self) -> list[LedgerEntry]:
        rows = self._db.execute(
            "SELECT seq, id, payload, prev_hash, hash FROM events ORDER BY seq"
        ).fetchall()
        return [self._entry_from_row(*row) for row in rows]

    def counts(self) -> dict:
        """Totals for an overview, from indexed columns (verify checks they match the events)."""
        by_op = {op: n for op, n in self._db.execute("SELECT op, COUNT(*) FROM events GROUP BY op") if op}
        return {
            "events": sum(by_op.values()),
            "by_op": by_op,
            "untrusted": self._db.execute("SELECT COUNT(*) FROM events WHERE trust = 'untrusted'").fetchone()[0],
            "snapshots": self._db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "head": self.head(),
            "head_seq": self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0],
        }

    def events_page(
        self, *, before_seq: int | None = None, limit: int = 50, op: str | None = None, trust: str | None = None
    ) -> list[LedgerEntry]:
        """Newest-first page of events. All filter values are checked against fixed lists and passed
        as bound parameters.
        """
        if not (isinstance(limit, int) and 1 <= limit <= 500):
            raise LedgerError("limit must be between 1 and 500")
        if before_seq is not None and not (isinstance(before_seq, int) and before_seq >= 1):
            raise LedgerError("before_seq must be a positive integer")
        if op is not None and op not in {o.value for o in Op}:
            raise LedgerError("unknown event kind")
        if trust is not None and trust not in {t.value for t in Trust}:
            raise LedgerError("unknown trust value")
        clauses: list[str] = []
        params: list = []
        for column, value in (("seq <", before_seq), ("op =", op), ("trust =", trust)):
            if value is not None:
                clauses.append(f"{column} ?")  # column text comes from this fixed tuple only
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.execute(
            f"SELECT seq, id, payload, prev_hash, hash FROM events{where} ORDER BY seq DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [self._entry_from_row(*row) for row in rows]

    def get_entry(self, event_id: str) -> LedgerEntry | None:
        if not (isinstance(event_id, str) and _EVENT_ID_RE.match(event_id)):
            raise LedgerError("an event id looks like e1, e2, e3 ...")
        row = self._db.execute(
            "SELECT seq, id, payload, prev_hash, hash FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return self._entry_from_row(*row) if row else None

    def verify(self, expected_head: str | None = None) -> VerifyResult:
        result = VerifyResult()
        rows = self._db.execute(
            "SELECT seq, id, backend, ref, snap, payload, prev_hash, hash, op, trust FROM events ORDER BY seq"
        ).fetchall()
        expected_seq = 1
        prev = GENESIS
        hash_by_seq: dict[int, str] = {0: GENESIS}
        recorded: dict[str, dict] = {}  # snapshot id -> what the chain says about it
        deleted: set[str] = set()
        for seq, event_id, backend, ref, snap, payload, stored_prev, stored_hash, op_col, trust_col in rows:
            if seq != expected_seq:
                result.problems.append(f"entries missing before seq {seq}")
                expected_seq = seq
            if event_id != f"e{seq}":
                result.problems.append(f"seq {seq} has a wrong id")
            if stored_prev != prev:
                result.problems.append(f"seq {seq} does not link to the entry before it")
            if compute_hash(prev, event_id, payload) != stored_hash:
                result.problems.append(f"seq {seq} was changed after it was written")
            else:
                self._check_event_content(result, seq, backend, ref, snap, payload, op_col, trust_col, recorded, deleted)
            hash_by_seq[seq] = stored_hash
            prev = stored_hash
            expected_seq += 1
        if expected_head is not None and prev != expected_head:
            result.problems.append(
                "ledger head differs from the expected head; newest entries may have been removed"
            )
        self._verify_snapshots(result, hash_by_seq, recorded, deleted)
        result.ok = not result.problems
        return result

    @staticmethod
    def _check_event_content(result, seq, backend, ref, snap, payload, op_col, trust_col, recorded, deleted) -> None:
        try:
            data = json.loads(payload)
            op = data.get("op")
            expected_snap = None
            if op in (Op.SNAPSHOT.value, Op.SNAPSHOT_DELETED.value):
                sid = str(data.get("memory_id", "")).removeprefix("snapshot:")
                expected_snap = f"{op}:{sid}"
                if op == Op.SNAPSHOT.value:
                    recorded[sid] = json.loads(data["after"])
                else:
                    deleted.add(sid)
            elif op == Op.ROLLBACK.value:
                rid = str(data.get("memory_id", ""))
                if not re.match(r"^rollback:r[1-9][0-9]{0,8}\Z", rid):
                    raise ValueError("bad rollback id")
                expected_snap = f"{op}:{rid}"
                validate_rollback_details(json.loads(data["after"]))
            if (data.get("backend") != backend or data.get("backend_ref") != ref or snap != expected_snap
                    or data.get("op") != op_col or data.get("trust") != trust_col):
                result.problems.append(f"seq {seq} index columns do not match its content")
        except (ValueError, AttributeError, KeyError, TypeError):
            result.problems.append(f"seq {seq} content is not readable")

    def _verify_snapshots(self, result, hash_by_seq, recorded, deleted) -> None:
        present: set[str] = set()
        for row in self._db.execute("SELECT id FROM snapshots ORDER BY id").fetchall():
            sid = f"s{row[0]}"
            present.add(sid)
            try:
                info, digest = self._snapshot_from_db(row[0])
            except SnapshotError as exc:
                result.problems.append(str(exc))
                continue
            chain = recorded.get(sid)
            if chain is None:
                result.problems.append(f"snapshot {sid} is not recorded in the ledger chain")
            elif not isinstance(chain, dict) or chain.get("hash") != digest:
                result.problems.append(f"snapshot {sid} does not match its record in the ledger chain")
            if sid in deleted:
                result.problems.append(f"snapshot {sid} was deleted in the ledger but still exists")
            if hash_by_seq.get(info.ledger_seq) != info.ledger_head:
                result.problems.append(
                    f"snapshot {sid} refers to ledger entries that no longer match (removed or rewritten?)"
                )
        for sid in recorded:
            if sid not in present and sid not in deleted:
                result.problems.append(f"snapshot {sid} is recorded in the ledger but is missing")
        for digest, text in self._db.execute("SELECT hash, text FROM blobs"):
            try:
                if _text_digest(text) != digest:
                    result.problems.append("a stored snapshot text does not match its hash")
            except SnapshotError:
                result.problems.append("a stored snapshot text is not valid text")
        orphaned = self._db.execute(
            "SELECT COUNT(*) FROM snapshot_entries WHERE hash NOT IN (SELECT hash FROM blobs)"
        ).fetchone()[0]
        if orphaned:
            result.problems.append(f"{orphaned} snapshot entries refer to texts that are missing")

    # -- snapshots -----------------------------------------------------------------------------------

    def _snapshot_from_db(self, number: int) -> tuple[SnapshotInfo, str]:
        """Read a snapshot's header and recompute its digest from the stored entries."""
        sid = f"s{number}"
        row = self._db.execute(
            "SELECT backend, scope, taken_at, ledger_seq, ledger_head, complete, label, count, entries_hash "
            "FROM snapshots WHERE id = ?", (number,),
        ).fetchone()
        if row is None:
            raise SnapshotError(f"snapshot {sid} does not exist")
        backend, scope_json, taken_at, ledger_seq, ledger_head, complete, label, count, stored = row
        try:
            scope = json.loads(scope_json)
            entries = [
                [mid, h, json.loads(sc)]
                for mid, h, sc in self._db.execute(
                    "SELECT memory_id, hash, scope FROM snapshot_entries WHERE snapshot_id = ?", (number,)
                )
            ]
            info = SnapshotInfo(
                id=sid, backend=backend, scope=scope, taken_at=taken_at, ledger_seq=ledger_seq,
                ledger_head=ledger_head, complete=bool(complete), label=label, count=count,
            )
        except (ValueError, ValidationError, TypeError) as exc:
            raise SnapshotError(f"snapshot {sid} is unreadable") from exc
        digest = snapshot_digest(
            number, backend, scope, taken_at, ledger_seq, ledger_head, bool(complete), label, entries
        )
        if digest != stored or count != len(entries):
            raise SnapshotError(f"snapshot {sid} was changed after it was saved")
        return info, digest

    def save_snapshot(
        self, backend: str, scope: dict[str, str], memories: Sequence[Memory], *, complete: bool,
        taken_at: datetime, label: str | None = None,
    ) -> SnapshotInfo:
        """Store the memories as they are right now, and chain the snapshot's hash into the ledger."""
        self._require_writable()
        if label is not None and (not label or len(label) > MAX_LABEL_CHARS or has_unsafe_chars(label)):
            raise SnapshotError(f"a label must be 1 to {MAX_LABEL_CHARS} printable characters")
        if taken_at.tzinfo is None:
            raise SnapshotError("the snapshot time must carry a timezone")
        if len(memories) > MAX_SNAPSHOT_MEMORIES:
            raise SnapshotError(f"too many memories for one snapshot (limit {MAX_SNAPSHOT_MEMORIES})")
        if sum(len(m.text) for m in memories) > MAX_SNAPSHOT_CHARS:
            raise SnapshotError("the memories are too large for one snapshot")
        ids = [m.id for m in memories]
        if len(set(ids)) != len(ids):
            raise SnapshotError("the memory list contains the same id twice")

        blobs: dict[str, str] = {}
        entries: list[list] = []
        for memory in sorted(memories, key=lambda m: m.id):
            digest = _text_digest(memory.text)
            blobs[digest] = memory.text
            entries.append([memory.id, digest, dict(memory.scope)])
        taken = taken_at.isoformat()
        try:
            with self._tx():
                head = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                ledger_seq, ledger_head = (head[0], head[1]) if head else (0, GENESIS)
                self._db.executemany("INSERT OR IGNORE INTO blobs (hash, text) VALUES (?, ?)", list(blobs.items()))
                cursor = self._db.execute(
                    "INSERT INTO snapshots (backend, scope, taken_at, ledger_seq, ledger_head, complete, label, "
                    "count, entries_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
                    (backend, _canonical_json(scope), taken, ledger_seq, ledger_head, int(complete), label,
                     len(entries)),
                )
                number = cursor.lastrowid
                if number is None:
                    raise SnapshotError("could not save the snapshot: no row id was returned")
                self._db.executemany(
                    "INSERT INTO snapshot_entries (snapshot_id, memory_id, hash, scope) VALUES (?, ?, ?, ?)",
                    [(number, mid, h, _canonical_json(sc)) for mid, h, sc in entries],
                )
                digest = snapshot_digest(number, backend, scope, taken, ledger_seq, ledger_head,
                                         complete, label, entries)
                self._db.execute("UPDATE snapshots SET entries_hash = ? WHERE id = ?", (digest, number))
                sid = f"s{number}"
                self._append_locked([MemoryEvent(
                    backend=backend, memory_id=f"snapshot:{sid}", op=Op.SNAPSHOT, ts=taken_at, scope=scope,
                    after=_canonical_json({"count": len(entries), "complete": bool(complete), "hash": digest}),
                )])
                info, _ = self._snapshot_from_db(number)
        except sqlite3.IntegrityError as exc:
            raise LedgerConflictError("write collided with another writer; nothing was written") from exc
        except (sqlite3.Error, ValidationError) as exc:
            raise SnapshotError(f"could not save the snapshot: {exc}") from exc
        return info

    def list_snapshots(self) -> list[SnapshotInfo]:
        infos: list[SnapshotInfo] = []
        for (number,) in self._db.execute("SELECT id FROM snapshots ORDER BY id").fetchall():
            infos.append(self._snapshot_from_db(number)[0])
        return infos

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        """Load a snapshot after checking it against its own hash, its texts, and the ledger chain."""
        number = parse_snapshot_id(snapshot_id)
        info, digest = self._snapshot_from_db(number)
        record = self._db.execute(
            "SELECT payload FROM events WHERE snap = ?", (f"{Op.SNAPSHOT.value}:{info.id}",)
        ).fetchone()
        try:
            chained = json.loads(json.loads(record[0])["after"])["hash"] if record else None
        except (ValueError, KeyError, TypeError):
            chained = None
        if chained != digest:
            raise SnapshotError(f"snapshot {info.id} does not match its record in the ledger chain")
        entries = self._db.execute(
            "SELECT memory_id, hash, scope FROM snapshot_entries WHERE snapshot_id = ? ORDER BY memory_id",
            (number,),
        ).fetchall()
        texts: dict[str, str] = {}
        wanted = sorted({h for _, h, _ in entries})
        for i in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[i:i + _IN_CHUNK]
            marks = ",".join("?" for _ in chunk)  # only placeholders are built; values are bound
            for digest_, text in self._db.execute(f"SELECT hash, text FROM blobs WHERE hash IN ({marks})", chunk):
                if _text_digest(text) != digest_:
                    raise SnapshotError(f"snapshot {info.id} contains a text that was changed")
                texts[digest_] = text
        try:
            memories = [Memory(id=mid, text=texts[h], scope=json.loads(sc)) for mid, h, sc in entries]
        except KeyError as exc:
            raise SnapshotError(f"snapshot {info.id} is missing a stored text") from exc
        except (ValueError, ValidationError) as exc:
            raise SnapshotError(f"snapshot {info.id} contains an unreadable entry") from exc
        return Snapshot(info=info, memories=memories)

    def delete_snapshot(self, snapshot_id: str) -> SnapshotInfo:
        """Remove a snapshot. The deletion is chained into the ledger, so it cannot be done silently.
        Texts no other snapshot uses are removed too.
        """
        self._require_writable()
        number = parse_snapshot_id(snapshot_id)
        try:
            with self._tx():
                info, _ = self._snapshot_from_db(number)
                self._db.execute("DELETE FROM snapshot_entries WHERE snapshot_id = ?", (number,))
                self._db.execute("DELETE FROM snapshots WHERE id = ?", (number,))
                self._db.execute("DELETE FROM blobs WHERE hash NOT IN (SELECT hash FROM snapshot_entries)")
                self._append_locked([MemoryEvent(
                    backend=info.backend, memory_id=f"snapshot:{info.id}", op=Op.SNAPSHOT_DELETED,
                    ts=datetime.now(timezone.utc), scope=info.scope,
                )])
        except sqlite3.IntegrityError as exc:
            raise LedgerConflictError("write collided with another writer; nothing was written") from exc
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not delete the snapshot: {exc}") from exc
        return info

    def record_rollback(self, backend: str, scope: dict[str, str], details: dict, *, ts: datetime) -> LedgerEntry:
        """Chain a record of a completed rollback into the ledger. Only this method can write one, so a rollback
        cannot be claimed (or hidden) by appending an ordinary event.
        """
        self._require_writable()
        if ts.tzinfo is None:
            raise LedgerError("the rollback time must carry a timezone")
        try:
            clean = validate_rollback_details(details)
        except ValueError as exc:
            raise LedgerError(f"invalid rollback record: {exc}") from exc
        try:
            with self._tx():
                number = self._db.execute("SELECT COUNT(*) FROM events WHERE op = ?", (Op.ROLLBACK.value,)).fetchone()[0] + 1
                return self._append_locked([MemoryEvent(
                    backend=backend, memory_id=f"rollback:r{number}", op=Op.ROLLBACK, ts=ts, scope=scope,
                    after=_canonical_json(clean),
                )])[0]
        except sqlite3.IntegrityError as exc:
            raise LedgerConflictError("write collided with another writer; nothing was written") from exc
        except sqlite3.Error as exc:
            raise LedgerError(f"could not record the rollback: {exc}") from exc
