from datetime import datetime, timezone

from memdebug.models import Memory, MemoryEvent, Op
from memdebug.reconcile import KnownMemory, baseline_events, find_external_changes, replay_events

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
U1 = {"user_id": "u1"}


def e(op, mid, before=None, after=None, scope=None):
    return MemoryEvent(backend="fake", memory_id=mid, op=op, ts=NOW, before=before, after=after, scope=scope or {})


def test_replay_follows_adds_updates_and_deletes():
    state = replay_events([
        e(Op.ADD, "a", after="one", scope=U1), e(Op.ADD, "b", after="two"),
        e(Op.UPDATE, "a", "one", "uno"), e(Op.DELETE, "b", before="two"),
    ])
    assert state == {"a": KnownMemory("uno", U1)}  # scope survives an update that carries none


def test_changes_outside_the_api_become_external_events():
    known = {"a": KnownMemory("short", U1), "b": KnownMemory("tea", U1)}
    live = [Memory(id="a", text="long", scope=U1), Memory(id="c", text="new", scope=U1)]
    found = {x.memory_id: x for x in find_external_changes(known, live, "fake", NOW, scope=U1)}
    assert set(found) == {"a", "b", "c"}
    assert (found["a"].before, found["a"].after) == ("short", "long")
    assert found["b"].after is None and found["c"].before is None
    assert all(x.op == Op.EXTERNAL and x.ts_observed for x in found.values())


def test_quiet_once_recorded_including_external_deletions():
    known = {"a": KnownMemory("one", U1), "b": KnownMemory("two", U1)}
    live = [Memory(id="a", text="edited", scope=U1)]
    found = find_external_changes(known, live, "fake", NOW, scope=U1)
    state = replay_events(found)  # the ledger would now hold these events
    merged = {**{k: v for k, v in known.items() if k not in state}, **state}
    merged.pop("b", None)  # the external delete removed it
    assert find_external_changes(merged, live, "fake", NOW, scope=U1) == []


def test_other_scopes_are_never_reported_deleted():
    known = {"mine": KnownMemory("x", U1), "theirs": KnownMemory("y", {"user_id": "u2"}),
             "unknown_scope": KnownMemory("z", {})}
    found = find_external_changes(known, [], "fake", NOW, scope=U1)
    assert [x.memory_id for x in found] == ["mine"]


def test_incomplete_listing_never_claims_deletions():
    known = {"a": KnownMemory("x", U1)}
    assert find_external_changes(known, [], "fake", NOW, scope=U1, complete=False) == []


def test_baseline_records_existing_memories_as_observed_adds():
    events = baseline_events([Memory(id="a", text="x", scope=U1)], "fake", NOW)
    assert events[0].op == Op.ADD and events[0].ts_observed
