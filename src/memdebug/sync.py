"""One sync: copy new backend history into the ledger, then look for changes that bypassed it.

Safety rules baked in:
  * history rows are deduplicated by the backend's own row id, so repeating a sync is harmless;
  * a change outside the API is only recorded if it is seen in two passes, so a write that is
    halfway through (store updated, history row not yet written) is not a false alarm;
  * no deletions are claimed from a listing that may be cut short, and no changes at all are
    claimed from a history that was only partly read;
  * history rows that were recorded earlier but have since disappeared are reported;
  * everything is appended in one transaction.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .adapters.base import HistoryRead, LiveMemories, MemoryAdapter
from .errors import LedgerConflictError
from .ledger import Ledger
from .models import MemoryEvent, Op, Source, derive_trust
from .reconcile import find_external_changes, replay_events
from .textsafe import safe_text


@dataclass
class SyncReport:
    """What one sync did.

    Attributes:
        history_events: New history rows copied into the ledger.
        external_events: Changes recorded as made outside the backend's API (stores that keep a history).
        observed_events: Changes recorded as seen between two looks (stores that keep no history).
        unconfirmed: Candidate changes that were not seen again on the second pass and so were not recorded.
        skipped_rows: History rows that could not be understood.
        reconcile_skipped: True when the history was only partly read, so changes outside the API were not checked.
        warnings: Notes for the person, including those from the history and live reads.
        live: The listing the final pass looked at.
    """

    history_events: int = 0
    external_events: int = 0
    observed_events: int = 0  # changes seen in a store that keeps no history
    unconfirmed: int = 0
    skipped_rows: int = 0
    reconcile_skipped: bool = False
    warnings: list[str] = field(default_factory=list)
    live: LiveMemories | None = None  # the listing the final pass looked at


def _key(event: MemoryEvent) -> tuple[str, str | None, str | None]:
    return (event.memory_id, event.before, event.after)


ROLLBACK_ACTOR = "memdebug rollback"


def _with_source(event: MemoryEvent, memory) -> MemoryEvent:
    """A change that adds or alters a memory carries what the backend says about where that memory came from."""
    if memory is None or memory.source is None or event.after is None or event.source is not None:
        return event
    return event.model_copy(update={"source": memory.source, "trust": derive_trust(memory.source)})


def _observed(event: MemoryEvent) -> MemoryEvent:
    """A change seen in a store that keeps no history: an ordinary ADD, UPDATE or DELETE, time-stamped when it was noticed."""
    op = Op.DELETE if event.after is None else Op.ADD if event.before is None else Op.UPDATE
    return event.model_copy(update={"op": op})


def _acknowledged(event: MemoryEvent, written: dict[str, str | None]) -> MemoryEvent:
    """An outside change that is exactly what a rollback just wrote becomes an ordinary change with a named actor."""
    if event.memory_id not in written or written[event.memory_id] != event.after:
        return event
    op = Op.DELETE if event.after is None else Op.ADD if event.before is None else Op.UPDATE
    return event.model_copy(update={"op": op, "source": Source(actor_id=ROLLBACK_ACTOR)})


def sync(
    adapter: MemoryAdapter,
    ledger: Ledger,
    scope: dict[str, str],
    *,
    now: datetime | None = None,
    settle_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    adopt_existing: bool = False,
    max_history_rows: int = 100_000,
    acknowledge: dict[str, str | None] | None = None,
) -> SyncReport:
    """Copies new backend history into the ledger, then records changes that bypassed it.

    Reads the backend's history and live listing, appends the resulting events to the ledger in one transaction, and
    returns a report. If another process writes to the ledger meanwhile, the sync is retried up to three times.

    Args:
        adapter: The reader for the store.
        ledger: The ledger to append to.
        scope: The scope of the store to compare against.
        now: The time to put on observed changes; defaults to the current time.
        settle_seconds: How long to wait between the two passes that must both see a change.
        sleep: The function used to wait (replaceable for tests).
        adopt_existing: On the very first sync of a store, record memories that have no history as observed ADDs
            instead of as changes made outside the API.
        max_history_rows: The most history rows to ask the adapter to read.
        acknowledge: Memory id -> the exact text (or None for removed) that this tool itself just wrote. A change
            that matches is recorded as an ordinary ADD, UPDATE or DELETE by "memdebug rollback", not as a change
            made outside the history, so a rollback does not raise a false alarm. Anything that differs is still
            reported.

    Returns:
        A SyncReport with the counts and warnings.

    Raises:
        LedgerConflictError: If the ledger changed under the sync on all three attempts.
    """
    last_error: LedgerConflictError | None = None
    for _ in range(3):  # another process may have written to the ledger meanwhile
        try:
            return _sync_once(
                adapter, ledger, scope, now or datetime.now(timezone.utc),
                settle_seconds, sleep, adopt_existing, max_history_rows, acknowledge or {},
            )
        except LedgerConflictError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _sync_once(adapter, ledger, scope, now, settle_seconds, sleep, adopt_existing, max_rows, acknowledge) -> SyncReport:
    known_refs = ledger.known_refs(adapter.name)
    had_events = ledger.has_events(adapter.name)

    def observe() -> tuple[list[MemoryEvent], list[MemoryEvent], HistoryRead, LiveMemories]:
        history = adapter.read_history(max_rows)  # history first, then the live listing
        live = adapter.list_memories(scope)
        scopes = {m.id: m.scope for m in live.memories}
        new: list[MemoryEvent] = []
        seen: set[str] = set()
        for event in history.events:
            ref = event.backend_ref
            if ref is None or ref in known_refs or ref in seen:
                continue
            seen.add(ref)
            if not event.scope and event.memory_id in scopes:
                event = event.model_copy(update={"scope": scopes[event.memory_id]})
            new.append(event)
        if history.truncated:
            candidates: list[MemoryEvent] = []  # a partial history cannot support any claim
        else:
            prior = [e.event for e in ledger.entries() if e.event.backend == adapter.name]
            state = replay_events(prior + new)
            candidates = find_external_changes(
                state, live.memories, adapter.name, now, scope=scope, complete=live.complete
            )
        return new, candidates, history, live

    report = SyncReport()
    new, candidates, history, live = observe()
    confirmed = candidates
    if candidates:
        sleep(settle_seconds)
        new, second, history, live = observe()
        wanted = {_key(c) for c in candidates}
        confirmed = [c for c in second if _key(c) in wanted]
        report.unconfirmed = len(candidates) - len(confirmed)
        if report.unconfirmed:
            report.warnings.append(
                f"{report.unconfirmed} change(s) were not seen twice and were not recorded; "
                "they will be checked again on the next sync"
            )

    if adopt_existing and not had_events:
        confirmed = [
            c.model_copy(update={"op": Op.ADD}) if c.before is None and c.after is not None else c
            for c in confirmed
        ]

    if acknowledge:
        confirmed = [_acknowledged(c, acknowledge) for c in confirmed]

    observed_only = "history" not in adapter.capabilities
    if observed_only:  # no history to bypass: a difference is simply a change seen between two looks
        confirmed = [_observed(c) for c in confirmed]
    by_id = {m.id: m for m in live.memories}
    confirmed = [_with_source(c, by_id.get(c.memory_id)) for c in confirmed]

    if history.truncated:
        report.reconcile_skipped = True
        report.warnings.append("history was only partly read, so changes outside the API were not checked")
    else:
        missing = known_refs - history.refs
        if missing:
            sample = ", ".join(safe_text(ref, 40) for ref in sorted(missing)[:3])
            report.warnings.append(
                f"{len(missing)} history row(s) recorded earlier are no longer in the backend "
                f"history (removed or scrubbed); for example: {sample}"
            )
    report.warnings += history.warnings + live.warnings
    report.skipped_rows = history.skipped
    report.live = live

    ledger.append_many(new + confirmed)
    report.history_events = len(new)
    if observed_only:
        report.observed_events = len(confirmed)
    else:
        report.external_events = len(confirmed)
    return report
