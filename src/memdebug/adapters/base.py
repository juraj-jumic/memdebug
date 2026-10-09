"""What a memory backend adapter must provide.

Defines the `MemoryAdapter` protocol and the two result types its methods return. Adapters only read. Everything they
return came from an untrusted store. (Rolling a store back is not part of this protocol: it is done by the separate
engines in restore.py and folder_restore.py.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Memory, MemoryEvent


@dataclass
class LiveMemories:
    """The memories a store holds right now, as listed by `MemoryAdapter.list_memories`.

    Attributes:
        memories: The memories that could be read.
        complete: Whether the listing covers the whole store.
        warnings: Human-readable notes about anything skipped or cut short.
    """

    memories: list[Memory]
    complete: bool  # False when the listing may be cut short, so absence proves nothing
    warnings: list[str] = field(default_factory=list)


@dataclass
class HistoryRead:
    """A store's change history, as read by `MemoryAdapter.read_history`.

    Attributes:
        events: The rows that were understood, as events.
        refs: Backend row ids of every row seen, whether or not it became an event.
        truncated: Whether the store holds more rows than were read.
        skipped: How many rows could not be understood and so are not in `events`.
        warnings: Human-readable notes about anything skipped or cut short.
    """

    events: list[MemoryEvent]  # valid rows, oldest first
    refs: set[str]  # backend row ids of every row seen, valid or not
    truncated: bool  # True when more rows exist than were read
    skipped: int = 0  # rows that could not be understood
    warnings: list[str] = field(default_factory=list)


class MemoryAdapter(Protocol):
    """A read-only view of one memory store.

    Attributes:
        name: The backend name recorded on every event and snapshot.
        capabilities: What the store can do. "history" means it keeps a change history of its own; without it, memdebug
            records changes it observes between two looks instead.
    """

    name: str
    capabilities: set[str]  # history, global_feed, provenance, restore

    def list_memories(self, scope: dict[str, str]) -> LiveMemories:
        """Lists the memories the store holds now within `scope`.

        Raises:
            AdapterError: If the scope does not fit this store or the store cannot be read at all.
        """
        ...

    def read_history(self, max_rows: int) -> HistoryRead:
        """Reads up to `max_rows` rows of the store's global change history, oldest first.

        Raises:
            AdapterError: If `max_rows` is not positive or the history cannot be read.
        """
        ...

    def history(self, memory_id: str) -> list[MemoryEvent]:
        """Returns the recorded changes to one memory, oldest first.

        Raises:
            AdapterError: If `memory_id` is not usable or the history cannot be read.
        """
        ...
