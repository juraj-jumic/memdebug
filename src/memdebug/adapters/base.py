"""What a memory backend adapter must provide. Restore (plan and apply) comes in milestone 4.

Adapters only read. Everything they return came from an untrusted store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Memory, MemoryEvent


@dataclass
class LiveMemories:
    memories: list[Memory]
    complete: bool  # False when the listing may be cut short, so absence proves nothing
    warnings: list[str] = field(default_factory=list)


@dataclass
class HistoryRead:
    events: list[MemoryEvent]  # valid rows, oldest first
    refs: set[str]  # backend row ids of every row seen, valid or not
    truncated: bool  # True when more rows exist than were read
    skipped: int = 0  # rows that could not be understood
    warnings: list[str] = field(default_factory=list)


class MemoryAdapter(Protocol):
    name: str
    capabilities: set[str]  # history, global_feed, provenance, restore

    def list_memories(self, scope: dict[str, str]) -> LiveMemories: ...

    def read_history(self, max_rows: int) -> HistoryRead: ...

    def history(self, memory_id: str) -> list[MemoryEvent]: ...
