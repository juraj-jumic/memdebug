"""Canonical records. Every adapter translates its backend's history into these.

Field sizes are bounded here so one hostile row cannot bloat the ledger.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .textsafe import MAX_TEXT_CHARS

MAX_ID_CHARS = 256
MAX_FIELD_CHARS = MAX_TEXT_CHARS + 256  # room for the cut marker
MAX_SCOPE_ITEMS = 8


def _check_scope(value: dict[str, str]) -> dict[str, str]:
    if len(value) > MAX_SCOPE_ITEMS:
        raise ValueError("too many scope entries")
    for key, item in value.items():
        if not key or len(key) > MAX_ID_CHARS or len(item) > MAX_ID_CHARS:
            raise ValueError("scope entry is empty or too long")
    return value


class Op(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    EXTERNAL = "EXTERNAL"  # change found with no matching API history entry
    SNAPSHOT = "SNAPSHOT"  # bookkeeping: a snapshot was taken (its content hash is chained here)
    SNAPSHOT_DELETED = "SNAP_DEL"  # bookkeeping: a snapshot was deleted
    ROLLBACK = "ROLLBACK"  # bookkeeping: a memory store was restored to a snapshot (what was done is chained here)


META_OPS = frozenset({Op.SNAPSHOT, Op.SNAPSHOT_DELETED, Op.ROLLBACK})  # about the ledger itself, not about memories


class Trust(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    UNKNOWN = "unknown"


class SourceKind(str, Enum):
    USER_MESSAGE = "user_message"
    TOOL_RESULT = "tool_result"
    CONSOLIDATION = "consolidation"
    UNKNOWN = "unknown"


class Source(BaseModel):
    """Where a memory write came from. Session fields are filled by a session-source adapter
    (milestone 7). actor_id and role are whatever the backend recorded; they are not trusted
    to decide the source kind."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    turn: int | None = None
    kind: SourceKind = SourceKind.UNKNOWN
    actor_id: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    role: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    # What was observed about where this came from, in words (a backend's own label, how close a chat was). Descriptive only:
    # it never decides the kind or the trust.
    note: str | None = Field(default=None, max_length=400)


def derive_trust(source: Source | None) -> Trust:
    """Simple rule: text a user wrote is trusted, text a tool returned is not."""
    if source is None:
        return Trust.UNKNOWN
    if source.kind == SourceKind.USER_MESSAGE:
        return Trust.TRUSTED
    if source.kind == SourceKind.TOOL_RESULT:
        return Trust.UNTRUSTED
    return Trust.UNKNOWN


class MemoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = Field(min_length=1, max_length=64)
    memory_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    op: Op
    ts: datetime
    ts_observed: bool = False  # True when ts is when we saw it, not when the backend wrote it
    backend_ref: str | None = Field(default=None, max_length=MAX_ID_CHARS)  # backend's own row id
    scope: dict[str, str] = Field(default_factory=dict)
    before: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)
    after: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)
    source: Source | None = None
    trust: Trust = Trust.UNKNOWN

    @field_validator("ts")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must carry a timezone")
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_scope(value)


class LedgerEntry(BaseModel):
    seq: int
    id: str
    event: MemoryEvent
    prev_hash: str
    hash: str


class Memory(BaseModel):
    """One memory as the backend holds it right now."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    text: str = Field(max_length=MAX_FIELD_CHARS)
    scope: dict[str, str] = Field(default_factory=dict)
    source: Source | None = None  # what the backend says about where it came from, when it says anything

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_scope(value)


class SnapshotInfo(BaseModel):
    """What is known about a snapshot without loading its memories."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^s[1-9][0-9]{0,8}$")
    backend: str = Field(min_length=1, max_length=64)
    scope: dict[str, str] = Field(default_factory=dict)
    taken_at: datetime
    ledger_seq: int = Field(ge=0)  # the ledger entry this snapshot was taken after
    ledger_head: str = Field(min_length=64, max_length=64)
    complete: bool  # False: the listing may have been cut short, so absence proves nothing
    label: str | None = Field(default=None, max_length=100)
    count: int = Field(ge=0)

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_scope(value)


class Snapshot(BaseModel):
    info: SnapshotInfo
    memories: list[Memory]
