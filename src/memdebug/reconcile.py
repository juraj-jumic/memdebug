"""Compare what the ledger says the store holds with what the backend holds now.

Differences become EXTERNAL events: changes that bypassed the backend's own API history.
Two rules keep this from raising false alarms:
  * deletions are only claimed for memories in the same scope as the live listing, and
  * deletions are never claimed from a listing that may be cut short.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .models import META_OPS, Memory, MemoryEvent, Op


@dataclass(frozen=True)
class KnownMemory:
    """A memory as the ledger's events say it is: its current text and its scope.

    Attributes:
        text: The text after the latest event for the memory.
        scope: The scope recorded for the memory, or empty when no event named one.
    """

    text: str
    scope: dict[str, str] = field(default_factory=dict)


def replay_events(events: Iterable[MemoryEvent]) -> dict[str, KnownMemory]:
    """Memory text per id, as the event stream says it is right now."""
    state: dict[str, KnownMemory] = {}
    for event in events:
        if event.op in META_OPS:
            continue  # snapshot bookkeeping says nothing about what a memory contains
        if event.op == Op.DELETE or event.after is None:
            state.pop(event.memory_id, None)
            continue
        previous = state.get(event.memory_id)
        scope = event.scope or (previous.scope if previous else {})
        state[event.memory_id] = KnownMemory(event.after, scope)
    return state


def scope_matches(memory_scope: dict[str, str], wanted: dict[str, str]) -> bool:
    """True only when the memory's scope is known and contains every wanted entry."""
    return bool(memory_scope) and all(memory_scope.get(k) == v for k, v in wanted.items())


def baseline_events(live: list[Memory], backend: str, now: datetime) -> list[MemoryEvent]:
    """First run on an existing store: record what is already there as observed ADDs."""
    return [
        MemoryEvent(
            backend=backend, memory_id=m.id, op=Op.ADD, ts=now, ts_observed=True,
            scope=m.scope, after=m.text,
        )
        for m in sorted(live, key=lambda m: m.id)
    ]


def find_external_changes(
    known: dict[str, KnownMemory],
    live: list[Memory],
    backend: str,
    now: datetime,
    *,
    scope: dict[str, str],
    complete: bool = True,
) -> list[MemoryEvent]:
    """EXTERNAL events for every difference between the known state and the live listing.

    A live memory that is new to the ledger, or whose text differs from the known text, gives an event with the
    old text in `before` (None for a new memory). A known memory that is missing from the live listing gives an
    event with `after` set to None, but only when `complete` is true and the memory's scope matches `scope`.

    Args:
        known: The state replayed from the ledger, as returned by replay_events().
        live: The memories the backend holds now.
        backend: The backend name to put on the events.
        now: The time to put on the events, which are marked as observed.
        scope: The scope of the live listing; deletions are only claimed inside it.
        complete: False when the live listing may have been cut short, which suppresses deletions.

    Returns:
        The events, with changed and new memories first, then deletions, each group ordered by memory id.
    """
    events: list[MemoryEvent] = []
    live_ids = {m.id for m in live}
    for m in sorted(live, key=lambda m: m.id):
        entry = known.get(m.id)
        if entry is None or entry.text != m.text:
            events.append(
                MemoryEvent(
                    backend=backend, memory_id=m.id, op=Op.EXTERNAL, ts=now, ts_observed=True,
                    scope=m.scope, before=entry.text if entry else None, after=m.text,
                )
            )
    if complete:
        in_scope = {i: k for i, k in known.items() if scope_matches(k.scope, scope)}
        for memory_id in sorted(set(in_scope) - live_ids):
            events.append(
                MemoryEvent(
                    backend=backend, memory_id=memory_id, op=Op.EXTERNAL, ts=now, ts_observed=True,
                    scope=in_scope[memory_id].scope, before=in_scope[memory_id].text, after=None,
                )
            )
    return events
