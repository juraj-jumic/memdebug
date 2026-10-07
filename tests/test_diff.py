import time
from datetime import datetime, timezone

import pytest

from memdebug.diff import MAX_DIFF_LINES, MAX_SHOWN_LINES, diff_snapshots, line_diff
from memdebug.errors import SnapshotError
from memdebug.models import Memory, Snapshot, SnapshotInfo

T = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
U1 = {"user_id": "u1"}


def snapshot(sid, texts, *, complete=True, backend="fake", scope=None):
    info = SnapshotInfo(id=sid, backend=backend, scope=scope or dict(U1), taken_at=T, ledger_seq=0,
                        ledger_head="0" * 64, complete=complete, count=len(texts))
    return Snapshot(info=info, memories=[Memory(id=k, text=v, scope=scope or dict(U1)) for k, v in texts.items()])


def kinds(diff):
    return [(c.kind, c.memory_id) for c in diff.changes]


def test_added_removed_and_changed_are_found_in_a_stable_order():
    old = snapshot("s1", {"b": "two", "a": "one", "c": "three"})
    new = snapshot("s2", {"a": "one", "b": "TWO", "d": "four"})
    diff = diff_snapshots(old, new)
    assert kinds(diff) == [("changed", "b"), ("removed", "c"), ("added", "d")]
    assert (diff.count("changed"), diff.count("added"), diff.count("removed")) == (1, 1, 1)
    change = diff.changes[0]
    assert (change.before, change.after) == ("two", "TWO") and diff.warnings == []


def test_identical_snapshots_have_no_changes():
    assert diff_snapshots(snapshot("s1", {"a": "x"}), snapshot("s2", {"a": "x"})).changes == []


def test_an_incomplete_older_snapshot_cannot_claim_additions():
    old = snapshot("s1", {"a": "x"}, complete=False)
    new = snapshot("s2", {"a": "y", "b": "new?"})
    diff = diff_snapshots(old, new)
    assert kinds(diff) == [("changed", "a")]  # a real change is still a real change
    assert len(diff.warnings) == 1 and "s1" in diff.warnings[0] and "additions" in diff.warnings[0]


def test_an_incomplete_newer_snapshot_cannot_claim_removals():
    old = snapshot("s1", {"a": "x", "b": "y"})
    new = snapshot("s2", {"a": "x"}, complete=False)
    diff = diff_snapshots(old, new)
    assert diff.changes == [] and "removals" in diff.warnings[0] and "s2" in diff.warnings[0]


def test_two_incomplete_snapshots_claim_only_real_changes():
    old = snapshot("s1", {"a": "x", "b": "y"}, complete=False)
    new = snapshot("s2", {"a": "z", "c": "w"}, complete=False)
    diff = diff_snapshots(old, new)
    assert kinds(diff) == [("changed", "a")] and len(diff.warnings) == 2


@pytest.mark.parametrize("other", [
    {"backend": "other"}, {"scope": {"user_id": "u2"}}, {"scope": {"user_id": "u1", "agent_id": "a"}},
])
def test_snapshots_of_different_stores_are_refused(other):
    with pytest.raises(SnapshotError, match="cannot be compared"):
        diff_snapshots(snapshot("s1", {"a": "x"}), snapshot("s2", {"a": "x"}, **other))


def test_line_diff_shows_what_changed_line_by_line():
    lines = line_diff("one\ntwo\nthree\n", "one\nTWO\nthree\n")
    assert "-two" in lines and "+TWO" in lines


def test_a_huge_adversarial_text_cannot_make_the_diff_slow_or_large():
    before = "\n".join(f"line {i}" for i in range(60_000))
    after = "\n".join(f"line {i % 977}" for i in range(60_000))  # many different lines
    started = time.monotonic()
    lines = line_diff(before, after)
    assert time.monotonic() - started < 10
    assert len(lines) <= MAX_SHOWN_LINES + 3
    assert any(f"first {MAX_DIFF_LINES} lines" in line for line in lines)


def test_line_diff_handles_missing_sides():
    assert "+only" in line_diff(None, "only") and "-gone" in line_diff("gone", None)
    assert line_diff("same", "same") == []
