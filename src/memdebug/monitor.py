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

from .backends import friendly_id
from .errors import MemdebugError
from .hints import Hint, new_hints, scan
from .ledger import Ledger
from .models import Op
from .stores import Registry, StoreConfig, open_store
from .sync import sync
from .textsafe import safe_text
from .witness import SAME_DISK_WARNING, WitnessError, append_witness, same_disk

VERBS = {Op.ADD: "added", Op.UPDATE: "edited", Op.DELETE: "removed", Op.EXTERNAL: "changed outside the history"}
MIN_INTERVAL = 5.0


@dataclass
class StoreResult:
    store: StoreConfig
    changes: list[tuple[Op, str]] = field(default_factory=list)
    outside_history: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    hints: list[tuple[str, Hint]] = field(default_factory=list)  # wording worth a second look in what changed
    previews: dict[str, str] = field(default_factory=dict)  # the start of what each changed memory says, to label opaque ids

    @property
    def attention(self) -> bool:
        return self.outside_history > 0

    @property
    def quiet(self) -> bool:
        return not self.changes and self.error is None


@dataclass
class CheckSummary:
    results: list[StoreResult]
    ledger_ok: bool
    ledger_problems: list[str] = field(default_factory=list)
    witness_note: str | None = None
    witness_warning: str | None = None

    @property
    def attention(self) -> bool:
        return not self.ledger_ok or any(r.attention for r in self.results)

    @property
    def hinted(self) -> bool:
        return any(r.hints for r in self.results)

    def exit_code_for(self, strict: bool = False) -> int:
        """1: something needs a look (with strict, also wording worth a second look); 2: a store could not be checked; else 0."""
        return 1 if (self.attention or (strict and self.hinted)) else 2 if self.failed else 0

    @property
    def failed(self) -> bool:
        return any(r.error for r in self.results)

    @property
    def exit_code(self) -> int:
        return 1 if self.attention else 2 if self.failed else 0


def _list(ids: list[str], limit: int = 3, previews: dict[str, str] | None = None) -> str:
    shown = ", ".join(friendly_id(i, (previews or {}).get(i)) for i in ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


def describe(result: StoreResult) -> str:
    name = safe_text(result.store.name, 40)
    if result.error:
        return f"  {name}: COULD NOT BE CHECKED: {result.error}"
    if result.quiet:
        return f"  {name}: quiet, nothing new"
    parts = []
    for op in (Op.EXTERNAL, Op.ADD, Op.UPDATE, Op.DELETE):
        ids = [i for o, i in result.changes if o == op]
        if ids:
            parts.append(f"{VERBS[op]}: {_list(ids, previews=result.previews)}")
    lead = "ATTENTION" if result.attention else f"{len(result.changes)} change{'' if len(result.changes) == 1 else 's'} noticed"
    return f"  {name}: {lead} ({'; '.join(parts)})"


def hint_lines(result: StoreResult) -> list[str]:
    shown = [f"    worth a second look: {friendly_id(mid, result.previews.get(mid))}: {safe_text(h.message, 120)}" for mid, h in result.hints[:3]]
    if len(result.hints) > 3:
        shown.append(f"    ... and {len(result.hints) - 3} more (see 'memdebug serve' or 'memdebug report')")
    return shown


def check_store(store: StoreConfig, ledger: Ledger, *, settle: float = 1.0) -> StoreResult:
    result = StoreResult(store)
    before = ledger.counts()["events"]
    try:
        opened = open_store(store)
        report = sync(opened.adapter, ledger, opened.scope, settle_seconds=settle)
    except MemdebugError as exc:
        result.error = safe_text(exc, 300)
        return result
    except Exception as exc:  # one store failing for an unexpected reason must not stop the others
        result.error = f"unexpected error ({type(exc).__name__})"
        return result
    result.warnings = [safe_text(w, 300) for w in opened.notes + report.warnings]
    result.outside_history = report.external_events
    for entry in ledger.entries()[before:]:
        event = entry.event
        if event.op in VERBS:
            result.changes.append((event.op, event.memory_id))
            result.previews[event.memory_id] = event.after if event.after is not None else (event.before or "")
        if event.op in (Op.ADD, Op.UPDATE, Op.EXTERNAL) and len(result.hints) < 20:
            result.hints += [(event.memory_id, h) for h in new_hints(event.before, event.after, budget=0.1, max_chars=20_000)
                             if h.severity == "warning"][:3]
    return result


def check_all(registry: Registry, ledger: Ledger, *, settle: float = 1.0, ledger_path: Path | None = None) -> CheckSummary:
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
    return summary


def summary_lines(summary: CheckSummary) -> list[str]:
    lines = [line for r in summary.results for line in [describe(r), *hint_lines(r)]]
    lines.append("  The ledger is intact." if summary.ledger_ok else "  PROBLEM: the ledger failed its integrity check: "
                 + (summary.ledger_problems[0] if summary.ledger_problems else ""))
    if summary.witness_note:
        lines.append(f"  {summary.witness_note}")
    return lines


# -- status: look, change nothing -----------------------------------------------------------------------------------------

def ago(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    for limit, unit, size in ((90, "second", 1), (5400, "minute", 60), (172800, "hour", 3600)):
        if seconds < limit:
            count = max(1, seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    days = seconds // 86400
    return f"{days} days ago"


def status_lines(registry: Registry, ledger: Ledger, *, now: datetime | None = None) -> list[str]:
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


# -- watch -----------------------------------------------------------------------------------------------------------------

def watch(registry: Registry, ledger: Ledger, *, every: float, say: Callable[[str], None], ring: Callable[[], None] = lambda: None,
          cycles: int | None = None, sleep: Callable[[float], None] = time.sleep, settle: float = 1.0, ledger_path: Path | None = None,
          clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    """Check every store again and again. Only things worth knowing are printed: changes, and an error when it first appears."""
    every = max(MIN_INTERVAL, every)
    say(f"Watching {len(registry.stores)} store(s) every {every:g} seconds. Press Ctrl+C to stop.")
    last_error: dict[str, str | None] = {}
    done = 0
    while cycles is None or done < cycles:
        summary = check_all(registry, ledger, settle=settle, ledger_path=ledger_path)
        stamp = clock().astimezone().strftime("%H:%M:%S")
        shown = False
        for result in summary.results:
            if result.error and last_error.get(result.store.name) == result.error:
                continue
            last_error[result.store.name] = result.error
            if not result.quiet:
                say(f"[{stamp}]{describe(result)}")
                for line in hint_lines(result):
                    say(line)
                shown = True
        if not summary.ledger_ok:
            say(f"[{stamp}]  PROBLEM: the ledger failed its integrity check")
            shown = True
        if shown and (summary.attention or summary.hinted):
            ring()
        done += 1
        if cycles is None or done < cycles:
            sleep(every)
