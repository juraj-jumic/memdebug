import pytest

from conftest import NOW, raw_row
from memdebug.adapters.mem0 import Mem0Adapter
from memdebug.errors import AdapterError, LedgerConflictError
from memdebug.ledger import Ledger
from memdebug.models import Op
from memdebug.sync import sync

SCOPE = {"user_id": "u1"}


def run(fake, ledger, **kw):
    kw.setdefault("settle_seconds", 0)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("now", NOW)
    return sync(Mem0Adapter(fake, fake.path, clock=lambda: NOW), ledger, SCOPE, **kw)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.db")


def ops(ledger):
    return [(e.event.op.value, e.event.memory_id) for e in ledger.entries()]


def test_normal_api_activity_produces_history_events_and_no_alarms(fake, ledger):
    fake.api_add("m1", "a", 0)
    fake.api_update("m1", "b", 10)
    report = run(fake, ledger)
    assert (report.history_events, report.external_events, report.warnings) == (2, 0, [])
    assert ops(ledger) == [("ADD", "m1"), ("UPDATE", "m1")]
    assert ledger.verify().ok


def test_repeating_a_sync_adds_nothing(fake, ledger):
    fake.api_add("m1", "a", 0)
    run(fake, ledger)
    head = ledger.head()
    report = run(fake, ledger)
    assert (report.history_events, report.external_events) == (0, 0) and ledger.head() == head


def test_an_edit_behind_the_api_is_recorded_once(fake, ledger):
    fake.api_add("m1", "Prefers short answers", 0)
    run(fake, ledger)
    fake.sneaky_edit("m1", "Prefers long answers")
    report = run(fake, ledger)
    assert report.external_events == 1
    external = ledger.entries()[-1].event
    assert external.op == Op.EXTERNAL and external.before == "Prefers short answers"
    assert external.after == "Prefers long answers" and external.ts_observed
    assert run(fake, ledger).external_events == 0  # not reported again


def test_a_delete_behind_the_api_is_recorded(fake, ledger):
    fake.api_add("m1", "a", 0)
    run(fake, ledger)
    fake.sneaky_delete("m1")
    assert run(fake, ledger).external_events == 1
    last = ledger.entries()[-1].event
    assert last.op == Op.EXTERNAL and last.after is None
    assert run(fake, ledger).external_events == 0


def test_a_memory_created_behind_the_api_is_recorded(fake, ledger):
    fake.api_add("m1", "a", 0)
    run(fake, ledger)
    fake.live["sneaky"] = {"memory": "planted", "user_id": "u1"}
    assert run(fake, ledger).external_events == 1
    assert ledger.entries()[-1].event.before is None


def test_a_write_caught_half_way_is_not_a_false_alarm(fake, ledger):
    fake.api_add("m1", "a", 0)
    run(fake, ledger)
    # mem0 updates the store first and writes the history row a moment later
    fake.live["m1"]["memory"] = "b"
    fake.after_get_all = lambda: raw_row(
        fake.path, memory_id="m1", old_memory="a", new_memory="b", event="UPDATE",
        created_at=fake.created["m1"], updated_at="2026-10-04T09:10:00+00:00",
    )
    report = run(fake, ledger)
    assert report.external_events == 0 and report.history_events == 1
    assert ops(ledger)[-1] == ("UPDATE", "m1")


def test_a_change_seen_once_is_not_recorded(fake, ledger):
    fake.api_add("m1", "a", 0)
    run(fake, ledger)
    fake.sneaky_edit("m1", "tampered")
    fake.after_get_all = lambda: fake.sneaky_edit("m1", "a")  # reverts before the second look
    report = run(fake, ledger)
    assert report.external_events == 0 and report.unconfirmed == 1
    assert any("not seen twice" in w for w in report.warnings)


def test_other_users_memories_are_not_treated_as_deleted(fake, ledger):
    fake.api_add("mine", "x", 0)
    fake.api_add("theirs", "y", 1, user="u2")
    sync(Mem0Adapter(fake, fake.path, clock=lambda: NOW), ledger, {"user_id": "u2"},
         settle_seconds=0, sleep=lambda s: None, now=NOW)
    assert run(fake, ledger).external_events == 0  # syncing u1 must not flag u2's memory


def test_an_incomplete_listing_never_creates_deletions(fake, ledger):
    for i in range(3):
        fake.api_add(f"m{i}", f"t{i}", i)
    run(fake, ledger)
    adapter = Mem0Adapter(fake, fake.path, live_limit=2, clock=lambda: NOW)
    report = sync(adapter, ledger, SCOPE, settle_seconds=0, sleep=lambda s: None, now=NOW)
    assert report.external_events == 0 and any("limit" in w for w in report.warnings)


def test_a_partly_read_history_makes_no_claims(fake, ledger):
    for i in range(4):
        fake.api_add(f"m{i}", f"t{i}", i)
    fake.sneaky_edit("m3", "tampered")
    report = run(fake, ledger, max_history_rows=2)
    assert report.reconcile_skipped and report.external_events == 0 and report.history_events == 2


def test_history_rows_that_disappear_are_reported(fake, ledger):
    fake.api_add("m1", "secret", 0)
    fake.api_delete("m1", 5)
    run(fake, ledger)
    import sqlite3
    conn = sqlite3.connect(fake.path)
    conn.execute("DELETE FROM history")  # someone scrubs the trail
    conn.commit()
    conn.close()
    report = run(fake, ledger)
    assert any("no longer in the backend history" in w for w in report.warnings)
    assert [e.event.after for e in ledger.entries()][:1] == ["secret"]  # the ledger still has it


def test_first_sync_can_adopt_memories_that_never_had_history(fake, ledger):
    fake.live["old"] = {"memory": "pre-existing", "user_id": "u1"}
    report = run(fake, ledger, adopt_existing=True)
    entry = ledger.entries()[0].event
    assert (entry.op, entry.ts_observed, report.external_events) == (Op.ADD, True, 1)
    fake.live["later"] = {"memory": "sneaky", "user_id": "u1"}
    run(fake, ledger, adopt_existing=True)
    assert ledger.entries()[-1].event.op == Op.EXTERNAL  # adoption only applies to the first sync


def test_without_adoption_unknown_memories_are_flagged(fake, ledger):
    fake.live["old"] = {"memory": "pre-existing", "user_id": "u1"}
    run(fake, ledger)
    assert ledger.entries()[0].event.op == Op.EXTERNAL


def test_a_collision_with_another_writer_is_retried(fake, ledger, monkeypatch):
    fake.api_add("m1", "a", 0)
    real = ledger.append_many
    calls = {"n": 0}

    def flaky(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LedgerConflictError("simulated")
        return real(events)

    monkeypatch.setattr(ledger, "append_many", flaky)
    assert run(fake, ledger).history_events == 1 and calls["n"] == 2
    assert ops(ledger) == [("ADD", "m1")]


def test_persistent_collisions_surface_as_an_error(fake, ledger, monkeypatch):
    fake.api_add("m1", "a", 0)

    def always(events):
        raise LedgerConflictError("simulated")

    monkeypatch.setattr(ledger, "append_many", always)
    with pytest.raises(LedgerConflictError):
        run(fake, ledger)


def test_nothing_is_written_if_the_backend_fails_midway(fake, ledger):
    fake.api_add("m1", "a", 0)

    def boom(**kw):
        raise RuntimeError("down")

    fake.get_all = boom
    with pytest.raises(AdapterError, match="down"):
        run(fake, ledger)
    assert ledger.entries() == []


def test_taking_and_deleting_snapshots_never_causes_alarms_or_warnings(fake, ledger):
    fake.api_add("m1", "a", 0)
    fake.api_add("m2", "b", 1)
    run(fake, ledger)
    live = Mem0Adapter(fake, fake.path, clock=lambda: NOW).list_memories(SCOPE)
    info = ledger.save_snapshot("mem0", SCOPE, live.memories, complete=live.complete, taken_at=NOW)
    report = run(fake, ledger)
    assert (report.external_events, report.history_events, report.warnings) == (0, 0, [])
    ledger.delete_snapshot(info.id)
    report = run(fake, ledger)
    assert (report.external_events, report.history_events, report.warnings) == (0, 0, [])
    assert ledger.verify().ok
