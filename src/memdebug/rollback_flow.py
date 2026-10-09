"""The steps around a markdown rollback, shared by the command line and the demo.

Restorer (adapters/restore.py) changes the files. This module wraps it so that a rollback is always recorded the same
way: the state before is snapshotted (that snapshot is the undo point), the files are restored, the state after is
snapshotted, and a ROLLBACK entry is chained into the ledger. The ledger record is checked BEFORE any file is touched,
so recording can never fail after the damage is done.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence

from .adapters.restore import Outcome, Plan
from .errors import MemdebugError
from .ledger import MAX_ROLLBACK_FILES, Ledger, validate_rollback_details
from .models import LedgerEntry, Snapshot, SnapshotInfo
from .sync import sync
from .textsafe import safe_text


class Restoring(Protocol):
    """What the flow needs from a restorer: the git engine (Restorer) and the plain-folder engine (FolderRestorer) both fit."""

    def plan(self, snapshot: Snapshot, *, only: Sequence[str] = ..., remove_added: bool = ...) -> Plan:
        """Work out what restoring the snapshot would change, without changing anything."""
        ...

    def apply(self, snapshot: Snapshot, *, expected_plan_id: str, only: Sequence[str] = ...,
              remove_added: bool = ...) -> Outcome:
        """Restore the snapshot, provided a fresh plan still has the confirmed plan's id."""
        ...


@dataclass
class RollbackResult:
    """What a finished rollback produced.

    Attributes:
        outcome: What the restorer did to the files.
        before: The snapshot taken just before the rollback, which is the undo point.
        after: The snapshot taken just after the rollback; None if recording it failed.
        entry: The ROLLBACK ledger entry; None if recording failed (see record_error).
        record_error: Why the ledger could not record the rollback. The files were restored all the same.
    """

    outcome: Outcome
    before: SnapshotInfo                      # the undo point
    after: SnapshotInfo | None = None
    entry: LedgerEntry | None = None          # None when recording failed (see record_error)
    record_error: str | None = None           # the files WERE restored, but the ledger could not record it


def rollback_details(snapshot: Snapshot, plan: Plan, before_id: str | None, after_id: str | None,
                     outcome: Outcome | None = None) -> dict:
    """Build the details dict that the ledger stores with a ROLLBACK entry.

    Args:
        snapshot: The snapshot being restored.
        plan: The confirmed plan; it supplies the previous head and, without an outcome, the file list.
        before_id: Id of the snapshot taken before the rollback, or None if not taken yet.
        after_id: Id of the snapshot taken after the rollback, or None if not taken yet.
        outcome: What the restorer did. Without it the commit and backup are None and the plan's items are listed.

    Returns:
        A dict with the target snapshot, both snapshot ids, the commit, the previous head, the backup, the file
        count and the first MAX_ROLLBACK_FILES files (path, action, source).
    """
    items = outcome.items if outcome else plan.items
    return {"target": snapshot.info.id, "before_snapshot": before_id, "after_snapshot": after_id,
            "commit": outcome.commit if outcome else None, "previous_head": plan.head,
            "backup": outcome.backup_ref if outcome else None, "file_count": len(items),
            "files": [{"path": i.path, "action": i.action, "source": i.source} for i in items[:MAX_ROLLBACK_FILES]]}


def run_rollback(adapter, ledger: Ledger, scope: dict[str, str], restorer: Restoring, snapshot: Snapshot, plan: Plan, *,
                 only: list[str] | None = None, remove_added: bool = False, settle: float = 1.0,
                 warn: Callable[[str], None] = lambda text: None) -> RollbackResult:
    """Carry out a confirmed plan and record it in the ledger.

    Checks that the ledger can hold the record, snapshots the current state (the undo point), has the restorer
    write the files, snapshots the state again and chains a ROLLBACK entry. Each snapshot runs a sync first, and
    the after-sync records what the rollback itself wrote as ordinary changes, not as outside ones.

    Args:
        adapter: Reader for the store being restored.
        ledger: Ledger that receives the snapshots and the ROLLBACK entry.
        scope: Scope of the store within the ledger.
        restorer: The engine that plans and writes the files.
        snapshot: The snapshot to restore.
        plan: The plan the person confirmed; its id is passed to the restorer, which refuses if it no longer fits.
        only: Paths to restore; all of the plan's files when empty or None.
        remove_added: Whether files added since the snapshot are removed.
        settle: Seconds each sync waits before its second look at a suspected outside change.
        warn: Called with each warning the syncs produce.

    Returns:
        The outcome and the snapshots. If the files were restored but the ledger could not record it, `entry` is
        None and `record_error` says why.

    Raises:
        MemdebugError: If the record could not be validated or the first snapshot could not be taken (nothing was
            changed), or if the restorer failed.
    """

    def record_state(note: str, written: dict | None = None) -> SnapshotInfo:
        report = sync(adapter, ledger, scope, settle_seconds=settle, acknowledge=written)
        for warning in report.warnings:
            warn(safe_text(warning, 300))
        if report.live is None:
            raise MemdebugError("the sync did not produce a listing to snapshot")
        return ledger.save_snapshot(adapter.name, scope, report.live.memories, complete=report.live.complete,
                                    taken_at=datetime.now(timezone.utc), label=note)

    try:  # the record is checked now, so recording can never fail after the files have been changed
        validate_rollback_details(rollback_details(snapshot, plan, None, None))
    except ValueError as exc:
        raise MemdebugError(f"this rollback could not be recorded ({safe_text(exc, 100)}), so it was not started") from exc

    before = record_state(f"before rollback to {snapshot.info.id}")  # this is what undoes the rollback
    outcome = restorer.apply(snapshot, expected_plan_id=plan.plan_id, only=list(only or []), remove_added=remove_added)
    result = RollbackResult(outcome=outcome, before=before)
    try:
        result.after = record_state(f"after rollback to {snapshot.info.id}",
                                    {i.path: (None if i.action == "remove" else i.target_text) for i in outcome.items})
        result.entry = ledger.record_rollback(adapter.name, scope, rollback_details(snapshot, plan, before.id, result.after.id, outcome),
                                              ts=datetime.now(timezone.utc))
    except MemdebugError as exc:
        result.record_error = safe_text(exc, 300)
    return result
