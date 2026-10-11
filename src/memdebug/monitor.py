"""Looking at every registered store: `check` (one pass), `status` (no changes made) and `watch` (repeat).

Each store is checked on its own: one that cannot be read is reported, and the others still run. Nothing here changes a
memory store; the only thing written is the ledger (and, if one is configured, the witness).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import laststatus
from .backends import friendly_id
from .errors import MemdebugError
from .hints import Hint, new_hints, scan
from .ledger import Ledger
from .models import LedgerEntry, MemoryEvent, Op
from .provenance import describe_explanation, explain_change
from .stores import Registry, StoreConfig, open_store
from .sync import sync
from .textsafe import safe_text
from .witness import SAME_DISK_WARNING, WitnessError, append_witness, same_disk

VERBS = {Op.ADD: "added", Op.UPDATE: "edited", Op.DELETE: "removed", Op.EXTERNAL: "changed outside the history"}
MIN_INTERVAL = 5.0
EXPLAINABLE_KINDS = ("folder", "markdown")  # stores whose notes are files an agent's edit tool could have written
MAX_EXPLAINED = 5                           # searches of the session logs in one pass over a store
MAX_SOURCES = 20                            # source descriptions kept from one pass over a store


@dataclass
class StoreResult:
    """What one pass over one store found.

    Attributes:
        store: The store that was checked.
        changes: The changes recorded in the ledger during this pass, as (operation, memory id).
        outside_history: How many changes were found that no history of the store explains.
        error: Why the store could not be checked; None if it could.
        warnings: Notes from opening the store and from the sync.
        hints: Wording worth a second look in what changed, as (memory id, hint).
        previews: For each changed memory id, the text it now holds (or held before, if removed), used to label ids.
        provenance: For some flagged notes, one line about which logged agent session wrote it (see provenance.py).
        sources: For added or edited memories of a store that describes where a write came from (Open WebUI: its own label and the timing of chats), that
            description in words. Evidence, never proof.
        memory_count: How many memories the store held at this look, or None if it could not be listed.
        incomplete: True if the pass did not see the whole store: some notes could not be read, or the history was too long to read in full.
        history_partial: True if the store's history was only partly read (a git store beyond the row or text limit). Then changes made outside git
            are not checked at all, and the newest history is the part that is missing.
    """

    store: StoreConfig
    changes: list[tuple[Op, str]] = field(default_factory=list)
    outside_history: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    hints: list[tuple[str, Hint]] = field(default_factory=list)  # wording worth a second look in what changed
    previews: dict[str, str] = field(default_factory=dict)  # the start of what each changed memory says, to label opaque ids
    provenance: dict[str, str] = field(default_factory=dict)  # which logged agent session wrote a flagged note, in words (see provenance.py)
    sources: dict[str, str] = field(default_factory=dict)  # what the store itself says about where a write came from, in words
    memory_count: int | None = None
    incomplete: bool = False
    history_partial: bool = False

    @property
    def attention(self) -> bool:
        """Whether something changed outside the store's history and needs a person's look."""
        return self.outside_history > 0

    @property
    def quiet(self) -> bool:
        """Whether the store was checked in full and nothing new was recorded. A store that could not be read in full is never quiet."""
        return not self.changes and self.error is None and not self.incomplete


@dataclass
class CheckSummary:
    """The outcome of checking every registered store, plus the state of the ledger itself.

    Attributes:
        results: One result per store, in registry order.
        ledger_ok: Whether the ledger's hash chain verified.
        ledger_problems: Up to five problems the verification reported, shortened for display.
        witness_note: What happened to the witness file; None if none is configured, the ledger did not verify, or
            the ledger holds no events.
        witness_warning: Set when the witness lives on the same disk as the ledger.
    """

    results: list[StoreResult]
    ledger_ok: bool
    ledger_problems: list[str] = field(default_factory=list)
    witness_note: str | None = None
    witness_warning: str | None = None

    @property
    def attention(self) -> bool:
        """Whether the ledger failed its check or any store has a change outside its history."""
        return not self.ledger_ok or any(r.attention for r in self.results)

    @property
    def hinted(self) -> bool:
        """Whether any store has wording worth a second look in what changed."""
        return any(r.hints for r in self.results)

    def exit_code_for(self, strict: bool = False) -> int:
        """1: something needs a look, or a store could not be read in full (with strict, also wording worth a second look); 2: a store could not be checked; else 0."""
        return 1 if (self.attention or self.incomplete or (strict and self.hinted)) else 2 if self.failed else 0

    @property
    def incomplete(self) -> bool:
        """Whether any store could not be read in full, so a quiet pass would not mean the memory is unchanged."""
        return any(r.incomplete for r in self.results)

    @property
    def failed(self) -> bool:
        """Whether any store could not be checked."""
        return any(r.error for r in self.results)

    @property
    def exit_code(self) -> int:
        """The process exit code without strict mode: 1 for attention or a store not read in full, 2 for a store that failed, else 0."""
        return 1 if (self.attention or self.incomplete) else 2 if self.failed else 0


def _list(ids: list[str], limit: int = 3, previews: dict[str, str] | None = None) -> str:
    """Name up to `limit` memories, with their previews to label opaque ids, and count the rest."""
    shown = ", ".join(friendly_id(i, (previews or {}).get(i)) for i in ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


def _why_incomplete(result: StoreResult) -> str:
    """Say in words why a pass did not see the whole store."""
    if result.history_partial:
        return "the history is too long to read in full, so edits made outside git are NOT being checked"
    return "some notes could not be read"


def describe(result: StoreResult) -> str:
    """Return the one-line summary of a store's pass: could not be checked, quiet, or the changes found."""
    name = safe_text(result.store.name, 40)
    if result.error:
        return f"  {name}: COULD NOT BE CHECKED: {result.error}"
    if result.quiet:
        return f"  {name}: quiet, nothing new"
    if result.incomplete and not result.changes:
        return f"  {name}: NOT READ IN FULL: nothing new in what could be read, but {_why_incomplete(result)}"
    parts = []
    for op in (Op.EXTERNAL, Op.ADD, Op.UPDATE, Op.DELETE):
        ids = [i for o, i in result.changes if o == op]
        if ids:
            parts.append(f"{VERBS[op]}: {_list(ids, previews=result.previews)}")
    lead = "ATTENTION" if result.attention else f"{len(result.changes)} change{'' if len(result.changes) == 1 else 's'} noticed"
    return f"  {name}: {lead} ({'; '.join(parts)})" + (f"; {_why_incomplete(result)}, so there may be more" if result.incomplete else "")


def hint_lines(result: StoreResult) -> list[str]:
    """Return display lines for up to three hints, plus a line counting any more; the hint text is shortened by `safe_text`."""
    shown = [f"    worth a second look: {friendly_id(mid, result.previews.get(mid))}: {safe_text(h.message, 120)}" for mid, h in result.hints[:3]]
    if len(result.hints) > 3:
        shown.append(f"    ... and {len(result.hints) - 3} more (see 'memdebug serve' or 'memdebug report')")
    return shown


def _search_start(earlier: list[LedgerEntry], event: MemoryEvent) -> datetime | None:
    """Find when to start looking for what wrote a changed note.

    That is the last time the ledger recorded anything about that note, else about its store. Both are earlier
    than the true last look, so the real write is never left out. None for a store seen for the first time.

    Args:
        earlier: The ledger entries from before this pass.
        event: The change being explained; its backend and scope pick the store's entries.
    """
    same_store = [e.event for e in earlier if e.event.backend == event.backend and e.event.scope == event.scope]
    return max((e.ts for e in same_store if e.memory_id == event.memory_id), default=None) or max((e.ts for e in same_store), default=None)


def provenance_lines(result: StoreResult) -> list[str]:
    """Return display lines saying who wrote up to three flagged notes, plus a line counting any more."""
    shown = [f"    who wrote it: {friendly_id(mid, result.previews.get(mid))}: {safe_text(text, 240)}" for mid, text in list(result.provenance.items())[:3]]
    if len(result.provenance) > 3:
        shown.append(f"    ... and {len(result.provenance) - 3} more")
    return shown


def source_lines(result: StoreResult) -> list[str]:
    """Return display lines saying what the store itself records about where up to three writes came from, plus a line counting any more.

    Open WebUI labels a memory the assistant saved itself exactly like one added by hand in Settings, so the lines say this is evidence, not proof.
    """
    if not result.sources:
        return []
    shown = [f"    where it came from: {friendly_id(mid, result.previews.get(mid))}: {safe_text(text, 300)}" for mid, text in list(result.sources.items())[:3]]
    if len(result.sources) > 3:
        shown.append(f"    ... and {len(result.sources) - 3} more")
    shown.append("    (the app's own label and timing: evidence, not proof. Open WebUI labels a memory the assistant saved itself the same as one added by hand)")
    return shown


def check_store(store: StoreConfig, ledger: Ledger, *, settle: float = 1.0) -> StoreResult:
    """Sync one store into the ledger and report what the sync recorded.

    Opens the store (an Open WebUI store in Docker gets a fresh copy of its database first), runs a sync, which
    appends to the ledger, and scans what changed for wording worth a second look. For file-based stores, a change
    outside the history or a flagged change may also get a search of the Claude Code session logs for what wrote it
    (at most MAX_EXPLAINED per pass). A store that cannot be read is reported in `error`, never raised.

    Args:
        store: The registered store to check.
        ledger: The ledger the sync writes to.
        settle: Seconds the sync waits before its second look at a suspected outside change.

    Returns:
        The changes, warnings, hints and provenance lines from this pass, or the error.
    """
    result = StoreResult(store)
    before = ledger.counts()["events"]
    try:
        opened = open_store(store)
        report = sync(opened.adapter, ledger, opened.scope, settle_seconds=settle)
    except MemdebugError as exc:
        result.error = safe_text(exc, 300)
        return result
    except Exception as exc:  # noqa: BLE001 - one store failing for an unexpected reason must not stop the others
        result.error = f"unexpected error ({type(exc).__name__})"
        return result
    result.warnings = [safe_text(w, 300) for w in opened.notes + report.warnings]
    result.outside_history = report.external_events
    result.history_partial = report.reconcile_skipped
    result.incomplete = result.history_partial or (report.live is not None and not report.live.complete)
    result.memory_count = len(report.live.memories) if report.live is not None else None
    entries = ledger.entries()
    for entry in entries[before:]:
        event = entry.event
        if event.op in VERBS:
            result.changes.append((event.op, event.memory_id))
            result.previews[event.memory_id] = event.after if event.after is not None else (event.before or "")
        if (event.op in (Op.ADD, Op.UPDATE) and event.source is not None and event.source.note
                and len(result.sources) < MAX_SOURCES and event.memory_id not in result.sources):
            who = ", ".join(part for part in (event.source.actor_id, event.source.role) if part)
            result.sources[event.memory_id] = f"{who}; {event.source.note}" if who else event.source.note
        flagged = [h for h in new_hints(event.before, event.after, budget=0.1, max_chars=20_000) if h.severity == "warning"][:3] \
            if event.op in (Op.ADD, Op.UPDATE, Op.EXTERNAL) and len(result.hints) < 20 else []
        result.hints += [(event.memory_id, h) for h in flagged]
        if (event.ts_observed and store.kind in EXPLAINABLE_KINDS and (event.op == Op.EXTERNAL or flagged)
                and len(result.provenance) < MAX_EXPLAINED and event.memory_id not in result.provenance):
            result.provenance[event.memory_id] = describe_explanation(
                explain_change(store.path, event.memory_id, _search_start(entries[:before], event), event.ts))
    return result


def check_all(registry: Registry, ledger: Ledger, *, settle: float = 1.0, ledger_path: Path | None = None) -> CheckSummary:
    """Check every store in the registry, verify the ledger, and update the witness if one is configured.

    Writes to the ledger (through each store's sync) and, only when the ledger verifies and holds events, appends
    to the witness file. A failing witness is reported in the summary, not raised.

    Args:
        registry: The stores to check, and the optional witness path.
        ledger: The ledger the syncs write to and that is verified.
        settle: Passed to each store's check.
        ledger_path: Where the ledger lives; with it, a witness on the same disk gets a warning.

    Returns:
        One result per store, the ledger verdict and the witness notes.
    """
    results = [check_store(store, ledger, settle=settle) for store in registry.stores]
    verdict = ledger.verify()
    summary = CheckSummary(results, verdict.ok, [safe_text(p, 300) for p in verdict.problems[:5]])
    if registry.witness and verdict.ok and ledger.counts()["events"]:
        try:
            line, new = append_witness(ledger, Path(registry.witness))
            summary.witness_note = f"witness updated (entry {line.seq})" if new else "witness already up to date"
            if ledger_path is not None and same_disk(ledger_path, Path(registry.witness)):
                summary.witness_warning = SAME_DISK_WARNING
        except WitnessError as exc:
            summary.witness_note = f"the witness could not be updated: {safe_text(exc, 200)}"
    if ledger_path is not None:  # for the viewer; a failure to write it never stops a check
        laststatus.write(laststatus.status_path(ledger_path), check_record(summary, datetime.now(timezone.utc)))
    return summary


def store_state(result: StoreResult) -> str:
    """One word for how a store's pass went: error, attention (a change outside its history), incomplete, changes or quiet."""
    if result.error:
        return "error"
    if result.attention:
        return "attention"
    if result.incomplete:
        return "incomplete"
    return "changes" if result.changes else "quiet"


def check_record(summary: CheckSummary, now: datetime) -> dict:
    """The latest check as the plain data `laststatus.write` saves for the viewer: the same words `check` printed, one entry per store."""
    stores = []
    for result in summary.results:
        details = [line.strip() for line in [*hint_lines(result), *provenance_lines(result), *source_lines(result)]]
        stores.append({"name": safe_text(result.store.name, 40), "kind": safe_text(result.store.kind, 20), "state": store_state(result),
                       "headline": describe(result).strip(), "lines": details[:laststatus.MAX_LINES], "memories": result.memory_count})
    return {"version": laststatus.VERSION, "checked_at": now.astimezone(timezone.utc).isoformat(), "ledger_ok": summary.ledger_ok,
            "ledger_problem": summary.ledger_problems[0] if summary.ledger_problems else None, "stores": stores[:laststatus.MAX_STORES],
            "note": summary.witness_note}


def summary_lines(summary: CheckSummary) -> list[str]:
    """Return the full report of a check as display lines: each store, then the ledger verdict and the witness note."""
    lines = [line for r in summary.results for line in [describe(r), *hint_lines(r), *provenance_lines(r), *source_lines(r)]]
    lines.append("  The ledger is intact." if summary.ledger_ok else "  PROBLEM: the ledger failed its integrity check: "
                 + (summary.ledger_problems[0] if summary.ledger_problems else ""))
    if summary.witness_note:
        lines.append(f"  {summary.witness_note}")
    return lines


# -- status: look, change nothing -----------------------------------------------------------------------------------------

def ago(then: datetime, now: datetime) -> str:
    """Return how long before `now` the time `then` was, in the largest sensible unit (seconds up to days)."""
    seconds = max(0, int((now - then).total_seconds()))
    for limit, unit, size in ((90, "second", 1), (5400, "minute", 60), (172800, "hour", 3600)):
        if seconds < limit:
            count = max(1, seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    days = seconds // 86400
    return f"{days} days ago"


def status_lines(registry: Registry, ledger: Ledger, *, now: datetime | None = None) -> list[str]:
    """Return one line per store: its size, last recorded entry and last snapshot, without recording anything.

    Reads the ledger and lists each store, but does not sync, so the ledger is unchanged. An Open WebUI store in
    Docker is read from the copy already on disk. A store that cannot be read gets a line saying so.

    Args:
        registry: The stores to describe.
        ledger: The ledger to read entries and snapshots from.
        now: The time ages are measured from; the current time when omitted.
    """
    now = now or datetime.now(timezone.utc)
    entries = ledger.entries()
    snapshots = ledger.list_snapshots()
    lines: list[str] = []
    for store in registry.stores:
        name = safe_text(store.name, 40)
        try:
            opened = open_store(store, refresh=False)  # status only looks: it reads the copy that is already there
            live = opened.adapter.list_memories(opened.scope)
            backend = opened.adapter.name
        except MemdebugError as exc:
            lines.append(f"  {name} ({store.kind}): cannot be read: {safe_text(exc, 200)}")
            continue
        mine = [e for e in entries if e.event.backend == backend and e.event.scope == opened.scope]
        outside = sum(1 for e in mine if e.event.op == Op.EXTERNAL)
        snaps = [s for s in snapshots if s.backend == backend and s.scope == opened.scope]
        last = f"last snapshot {snaps[-1].id}, {ago(snaps[-1].taken_at, now)}" if snaps else "no snapshot yet (take one: memdebug snapshot ...)"
        seen = f"last entry recorded {ago(mine[-1].event.ts, now)}" if mine else "nothing recorded yet"
        flag = f"; {outside} change(s) outside the history on record" if outside else ""
        partial = " (listing may be incomplete)" if not live.complete else ""
        lines.append(f"  {name} ({store.kind}): {len(live.memories)} memories{partial}; {seen}; {last}{flag}")
    return lines


# -- what a store holds right now ---------------------------------------------------------------------------------------

def store_hints(store: StoreConfig, *, limit: int = 10, budget: float = 3.0, refresh: bool = True) -> list[tuple[str, Hint]]:
    """Wording worth a second look in what a store holds now, so a planted phrase that is already there is not missed."""
    opened = open_store(store, refresh=refresh)
    found: list[tuple[str, Hint]] = []
    deadline = time.monotonic() + budget
    for memory in opened.adapter.list_memories(opened.scope).memories:
        if time.monotonic() > deadline or len(found) >= limit:
            break
        found += [(friendly_id(memory.id, memory.text), h) for h in scan(memory.text, budget=0.05, max_chars=20_000) if h.severity == "warning"][:2]
    return found[:limit]


# -- snapshots a person asks for -------------------------------------------------------------------------------------------

def alarms_since_snapshot(ledger: Ledger, backend: str, scope: dict[str, str], *, limit: int = 50) -> list[tuple[str, str]]:
    """List what a person should look at before a new snapshot is taken and later trusted as "good".

    These are the ledger's records about one store since its latest snapshot: changes made outside the store's own
    history, and added or edited memories whose wording looks suspicious. They are worked out from the ledger every
    time, so they do not go away once shown (or looked past). Reads the ledger only.

    Args:
        ledger: The ledger to read.
        backend: The backend name of the store.
        scope: The store's scope within the ledger.
        limit: The most findings to return.

    Returns:
        (memory id, reason) pairs in ledger order; one per flagged entry, with the first warning's message for
        suspicious wording.
    """
    snapshots = [info for info in ledger.list_snapshots() if info.backend == backend and info.scope == scope]
    start = max((info.ledger_seq for info in snapshots), default=0)
    found: list[tuple[str, str]] = []
    for entry in ledger.entries()[start:]:
        event = entry.event
        if event.backend != backend or event.scope != scope:
            continue
        if event.op == Op.EXTERNAL:
            found.append((event.memory_id, "changed outside the store's own history"))
        elif event.op in (Op.ADD, Op.UPDATE):
            hints = [h for h in new_hints(event.before, event.after, budget=0.1, max_chars=20_000) if h.severity == "warning"]
            if hints:
                found.append((event.memory_id, hints[0].message))
        if len(found) >= limit:
            break
    return found


# -- baseline ---------------------------------------------------------------------------------------------------------

def baseline(store: StoreConfig, ledger: Ledger, label: str = "baseline", *, refresh: bool = True) -> str:
    """Record what the store holds now and save a snapshot of it. Returns the snapshot id."""
    opened = open_store(store, refresh=refresh)
    report = sync(opened.adapter, ledger, opened.scope, settle_seconds=0.0, adopt_existing=True)
    if report.live is None:
        raise MemdebugError("the store could not be listed")
    info = ledger.save_snapshot(opened.adapter.name, opened.scope, report.live.memories, complete=report.live.complete,
                                taken_at=datetime.now(timezone.utc), label=label)
    return info.id


def _reconfigured(before: Registry, after: Registry) -> list[str]:
    """Names of stores that are in both lists but are now set up differently (another path, user, files...)."""
    old = {s.name: s for s in before.stores}
    return sorted(s.name for s in after.stores if s.name in old and old[s.name] != s)


def _list_change(before: Registry, after: Registry) -> str:
    """Say in words what changed in the list of watched stores, naming the stores (their names are plain by construction)."""
    old, new = {s.name for s in before.stores}, {s.name for s in after.stores}
    parts = []
    for label, names in (("now watching", sorted(new - old)), ("NO LONGER WATCHING", sorted(old - new)), ("set up differently", _reconfigured(before, after))):
        if names:
            parts.append(f"{label} {', '.join(safe_text(n, 40) for n in names[:5])}" + (f" and {len(names) - 5} more" if len(names) > 5 else ""))
    return "The list of watched stores changed: " + "; ".join(parts or ["its order or other details"]) + f" ({len(after.stores)} store(s))."


# -- watch -----------------------------------------------------------------------------------------------------------------

def watch(registry: Registry, ledger: Ledger, *, every: float, say: Callable[[str], None], ring: Callable[[], None] = lambda: None,
          cycles: int | None = None, sleep: Callable[[float], None] = time.sleep, settle: float = 1.0, ledger_path: Path | None = None,
          clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), reload: Callable[[], Registry] | None = None) -> None:
    """Check every store again and again. Only things worth knowing are printed: changes, and an error or a store not read in full when it first appears.

    With `reload`, the list of stores is read again before every look, so a store added while this runs is picked up; a settings file that cannot
    be read at that moment never stops the watching, the previous list is kept.
    """
    every = max(MIN_INTERVAL, every)
    say(f"Watching {len(registry.stores)} store(s) every {every:g} seconds. Press Ctrl+C to stop.")
    last_error: dict[str, str | None] = {}
    last_incomplete: dict[str, bool] = {}
    ledger_was_ok = True
    done = 0
    while cycles is None or done < cycles:
        if reload is not None:
            try:
                fresh = reload()
            except MemdebugError:
                fresh = registry
            if fresh.stores != registry.stores:
                say(f"[{clock().astimezone().strftime('%H:%M:%S')}]  {_list_change(registry, fresh)}")
                if {s.name for s in registry.stores} - {s.name for s in fresh.stores} or _reconfigured(registry, fresh):
                    ring()  # a store that is no longer watched, or watched differently, is what someone hiding a change would do: not a quiet line
            registry = fresh
        summary = check_all(registry, ledger, settle=settle, ledger_path=ledger_path)
        stamp = clock().astimezone().strftime("%H:%M:%S")
        shown = False
        for result in summary.results:
            if result.error and last_error.get(result.store.name) == result.error:
                continue
            last_error[result.store.name] = result.error
            said_incomplete = last_incomplete.get(result.store.name, False)
            last_incomplete[result.store.name] = result.incomplete
            if result.incomplete and said_incomplete and not result.changes:
                continue  # said once when it began, not again at every look
            if not result.quiet:
                say(f"[{stamp}]{describe(result)}")
                for line in [*hint_lines(result), *provenance_lines(result), *source_lines(result)]:
                    say(line)
                shown = True
        if not summary.ledger_ok and ledger_was_ok:  # said once when it starts to fail, not at every look
            say(f"[{stamp}]  PROBLEM: the ledger failed its integrity check")
            shown = True
        ledger_was_ok = summary.ledger_ok
        if shown and (summary.attention or summary.hinted or summary.incomplete):
            ring()
        done += 1
        if cycles is None or done < cycles:
            sleep(every)
