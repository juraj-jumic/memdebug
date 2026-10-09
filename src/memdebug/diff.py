"""Compare two snapshots.

Rules that keep the result honest:
  * only snapshots of the same backend and scope can be compared;
  * a memory that exists on both sides and differs is always a real change;
  * "added" needs the OLDER snapshot to be complete (otherwise the memory may simply not have been
    listed then), and "removed" needs the NEWER one to be complete;
  * when a claim cannot be made, it is left out and counted in a warning, never guessed.

Memory text is untrusted; nothing here interprets it. Line diffs are bounded so that a huge or
adversarial text cannot make the comparison slow.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from .errors import SnapshotError
from .models import Snapshot, SnapshotInfo

MAX_DIFF_LINES = 2000
MAX_SHOWN_LINES = 200


@dataclass(frozen=True)
class Change:
    kind: str  # "added", "removed" or "changed"
    memory_id: str
    before: str | None
    after: str | None


@dataclass
class Diff:
    old: SnapshotInfo
    new: SnapshotInfo
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.changes if c.kind == kind)


def diff_snapshots(old: Snapshot, new: Snapshot) -> Diff:
    if old.info.backend != new.info.backend or old.info.scope != new.info.scope:
        raise SnapshotError("snapshots of different backends or scopes cannot be compared")
    before = {m.id: m.text for m in old.memories}
    after = {m.id: m.text for m in new.memories}
    result = Diff(old=old.info, new=new.info)
    hidden_added = hidden_removed = 0
    for memory_id in sorted(set(before) | set(after)):
        in_old, in_new = memory_id in before, memory_id in after
        if in_old and in_new:
            if before[memory_id] != after[memory_id]:
                result.changes.append(Change("changed", memory_id, before[memory_id], after[memory_id]))
        elif in_new:
            if old.info.complete:
                result.changes.append(Change("added", memory_id, None, after[memory_id]))
            else:
                hidden_added += 1
        else:
            if new.info.complete:
                result.changes.append(Change("removed", memory_id, before[memory_id], None))
            else:
                hidden_removed += 1
    if hidden_added:
        result.warnings.append(
            f"{old.info.id} was taken from a listing that may have been cut short, so {hidden_added} "
            "memory(ies) may not really be new; additions were left out"
        )
    if hidden_removed:
        result.warnings.append(
            f"{new.info.id} was taken from a listing that may have been cut short, so {hidden_removed} "
            "memory(ies) may not really be gone; removals were left out"
        )
    return result


def line_diff(before: str | None, after: str | None) -> list[str]:
    """A unified line diff of two texts, bounded in size. Lines are returned raw: the caller must
    escape them before printing.
    """
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    notes: list[str] = []
    if len(a) > MAX_DIFF_LINES or len(b) > MAX_DIFF_LINES:
        a, b = a[:MAX_DIFF_LINES], b[:MAX_DIFF_LINES]
        notes.append(f"(only the first {MAX_DIFF_LINES} lines were compared)")
    lines = [line for line in difflib.unified_diff(a, b, "before", "after", n=1, lineterm="")]
    if len(lines) > MAX_SHOWN_LINES:
        lines = lines[:MAX_SHOWN_LINES] + [f"(diff shortened to {MAX_SHOWN_LINES} lines)"]
    return lines + notes
