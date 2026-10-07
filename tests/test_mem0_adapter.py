import hashlib
import sqlite3
from datetime import timedelta

import pytest

from conftest import BASE, NOW, FakeMemory, iso, raw_row
from memdebug.adapters.mem0 import Mem0Adapter, ensure_mem0_telemetry_off, validate_scope
from memdebug.errors import AdapterError, UnsupportedSchemaError
from memdebug.models import Op
from memdebug.textsafe import MAX_TEXT_CHARS


def adapter_for(fake, **kw):
    return Mem0Adapter(fake, fake.path, clock=lambda: NOW, **kw)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# -- reading the history file ------------------------------------------------------------

def test_events_follow_mem0_conventions_for_timestamps(fake):
    fake.api_add("m1", "Works at Acme", 0)
    fake.api_update("m1", "Works at Initech", 30)
    fake.api_delete("m1", 45)
    read = adapter_for(fake).read_history(100)
    assert [(e.op, e.before, e.after) for e in read.events] == [
        (Op.ADD, None, "Works at Acme"),
        (Op.UPDATE, "Works at Acme", "Works at Initech"),
        (Op.DELETE, "Works at Initech", None),
    ]
    # UPDATE/DELETE rows hold the memory's creation time in created_at; the event time is updated_at.
    assert [e.ts for e in read.events] == [BASE, BASE + timedelta(minutes=30), BASE + timedelta(minutes=45)]
    assert not read.truncated and read.skipped == 0 and read.warnings == []
    assert len(read.refs) == 3 and all(e.backend_ref in read.refs for e in read.events)


def test_events_in_the_same_second_are_ordered_by_full_timestamp(fake):
    raw_row(fake.path, memory_id="m1", new_memory="first", created_at=iso(0), updated_at=iso(0))
    raw_row(fake.path, memory_id="m1", old_memory="first", new_memory="third", event="UPDATE",
            created_at=iso(0), updated_at="2026-10-04T10:00:00.900000+00:00")
    raw_row(fake.path, memory_id="m1", old_memory="first", new_memory="second", event="UPDATE",
            created_at=iso(0), updated_at="2026-10-04T10:00:00.100000+00:00")
    texts = [e.after for e in adapter_for(fake).read_history(100).events]
    assert texts == ["first", "second", "third"]


def test_timestamp_variants_and_bad_timestamps(fake):
    raw_row(fake.path, memory_id="naive", new_memory="x", created_at="2026-10-04T10:00:00", updated_at=None)
    raw_row(fake.path, memory_id="zulu", new_memory="x", created_at="2026-10-04T10:00:00Z", updated_at=None)
    raw_row(fake.path, memory_id="junk", new_memory="x", created_at="not a date", updated_at=None)
    raw_row(fake.path, memory_id="blob", new_memory="x", created_at=b"\x00\x01", updated_at=None)
    raw_row(fake.path, memory_id="future", new_memory="x", created_at="2999-01-01T00:00:00+00:00")
    read = adapter_for(fake).read_history(100)
    by_id = {e.memory_id: e for e in read.events}
    assert by_id["naive"].ts == by_id["zulu"].ts and not by_id["naive"].ts_observed
    assert by_id["junk"].ts_observed and by_id["blob"].ts_observed and by_id["junk"].ts == NOW
    assert any("future" in w for w in read.warnings)


def test_hostile_rows_are_skipped_with_warnings_not_crashes(fake):
    path = fake.path
    raw_row(path, memory_id="ok", new_memory="fine")
    raw_row(path, memory_id=None, new_memory="no id")
    raw_row(path, memory_id="m" * 500, new_memory="id too long")
    raw_row(path, memory_id="x", new_memory="bad event", event="MERGE")
    raw_row(path, memory_id="x", new_memory="null event", event=None)
    raw_row(path, memory_id="x", new_memory=b"\x00\xffblob")
    raw_row(path, memory_id="x", old_memory=12345, new_memory="number")
    raw_row(path, memory_id="lower", new_memory="accepted", event="add")
    read = adapter_for(fake).read_history(100)
    # a number in a TEXT column is stored as text by SQLite, so only the BLOB row is "not text"
    assert sorted(e.memory_id for e in read.events) == ["lower", "ok", "x"]
    assert read.skipped == 5 and len(read.warnings) == 5
    assert len(read.refs) == 8  # skipped rows still count as present


def test_invalid_utf8_in_a_row_does_not_break_the_read(fake):
    conn = sqlite3.connect(fake.path)
    conn.execute(
        "INSERT INTO history (id, memory_id, new_memory, event, created_at) "
        "VALUES ('bad', 'm1', CAST(X'ff61' AS TEXT), 'ADD', ?)", (iso(0),)
    )
    conn.commit()
    conn.close()
    read = adapter_for(fake).read_history(100)
    assert read.events[0].after == "\ufffda"


def test_huge_text_is_cut_but_changes_beyond_the_cut_still_differ(fake):
    head = "a" * MAX_TEXT_CHARS
    raw_row(fake.path, memory_id="m1", new_memory=head + "one")
    raw_row(fake.path, memory_id="m2", new_memory=head + "two")
    one, two = adapter_for(fake).read_history(100).events
    assert len(one.after) < MAX_TEXT_CHARS + 200 and one.after != two.after


def test_a_flood_of_bad_rows_cannot_flood_the_warnings(fake):
    conn = sqlite3.connect(fake.path)
    conn.executemany(
        "INSERT INTO history (id, memory_id, event) VALUES (?, ?, 'BOGUS')",
        [(f"id{i}", f"m{i}") for i in range(500)],
    )
    conn.commit()
    conn.close()
    read = adapter_for(fake).read_history(1000)
    assert read.skipped == 500 and len(read.warnings) <= 51 and "more warnings" in read.warnings[-1]


def test_row_limit_is_enforced_and_reported(fake):
    for i in range(5):
        fake.api_add(f"m{i}", f"t{i}", i)
    read = adapter_for(fake).read_history(2)
    assert read.truncated and len(read.events) == 2 and any("more than 2 rows" in w for w in read.warnings)
    with pytest.raises(AdapterError):
        adapter_for(fake).read_history(0)


def test_sql_looking_values_are_just_data(fake):
    evil = "x'; DROP TABLE history; --"
    raw_row(fake.path, memory_id=evil, new_memory="'); DELETE FROM history; --")
    adapter = adapter_for(fake)
    read = adapter.read_history(10)
    assert read.events[0].memory_id == evil
    assert adapter.history(evil)[0].after == "'); DELETE FROM history; --"
    assert len(adapter.read_history(10).events) == 1  # table still there, row still there


def test_actor_and_role_are_recorded_but_do_not_decide_trust(fake):
    raw_row(fake.path, memory_id="m1", new_memory="x", actor_id="alice", role="user")
    event = adapter_for(fake).read_history(10).events[0]
    assert event.source.actor_id == "alice" and event.source.role == "user"
    assert event.source.kind.value == "unknown" and event.trust.value == "unknown"


# -- never touching the file ------------------------------------------------------------------

def test_reading_never_changes_the_file_and_the_connection_cannot_write(fake):
    fake.api_add("m1", "x", 0)
    before = sha(fake.path)
    adapter = adapter_for(fake)
    adapter.read_history(10)
    adapter.history("m1")
    assert sha(fake.path) == before
    import os
    assert not any(n.endswith(("-journal", "-wal", "-shm")) for n in os.listdir(os.path.dirname(fake.path)))
    conn = adapter._connect_ro()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO history (id, memory_id) VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DROP TABLE history")
    finally:
        conn.close()
    assert sha(fake.path) == before


def test_wrong_paths_and_wrong_files_fail_clearly_without_creating_anything(fake, tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(AdapterError):
        Mem0Adapter(fake, missing)
    assert not missing.exists()
    with pytest.raises(AdapterError):
        Mem0Adapter(fake, tmp_path)  # a directory
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite" * 50)
    with pytest.raises(AdapterError):
        Mem0Adapter(fake, junk)
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE events (seq INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(UnsupportedSchemaError, match="no 'history' table"):
        Mem0Adapter(fake, other)
    partial = tmp_path / "partial.db"
    conn = sqlite3.connect(partial)
    conn.execute("CREATE TABLE history (id TEXT, memory_id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(UnsupportedSchemaError, match="lacks expected columns"):
        Mem0Adapter(fake, partial)


def test_schema_without_optional_columns_still_reads(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT, "
                 "event TEXT, created_at DATETIME, updated_at DATETIME)")
    conn.execute("INSERT INTO history VALUES ('1', 'm1', NULL, 'x', 'ADD', ?, ?)", (iso(0), iso(0)))
    conn.commit()
    conn.close()
    fake = FakeMemory.__new__(FakeMemory)
    fake.get_all = lambda **k: {"results": []}
    fake.history = lambda mid: []
    assert Mem0Adapter(fake, path, clock=lambda: NOW).read_history(10).events[0].source is None


def test_objects_that_are_not_mem0_are_refused(fake):
    with pytest.raises(AdapterError):
        Mem0Adapter(object(), fake.path)
    with pytest.raises(AdapterError):
        Mem0Adapter(fake, fake.path, live_limit=0)


# -- live listing ------------------------------------------------------------------------------

def test_listing_uses_the_public_api_with_expired_included_and_marks_scope(fake):
    fake.api_add("m1", "a", 0)
    live = adapter_for(fake).list_memories({"user_id": "u1"})
    assert fake.get_all_calls == [{"filters": {"user_id": "u1"}, "top_k": 10_000, "show_expired": True}]
    assert live.complete and live.memories[0].scope == {"user_id": "u1"}


def test_scope_misuse_is_refused(fake):
    adapter = adapter_for(fake)
    for bad in [{}, None, "u1", {"user": "u1"}, {"user_id": ""}, {"user_id": 5}, {"user_id": "u" * 300}]:
        with pytest.raises(AdapterError):
            adapter.list_memories(bad)
    assert validate_scope({"user_id": "u1", "agent_id": "a"}) == {"user_id": "u1", "agent_id": "a"}


def test_a_full_listing_is_not_trusted_to_be_complete(fake):
    for i in range(3):
        fake.api_add(f"m{i}", f"t{i}", i)
    live = adapter_for(fake, live_limit=3).list_memories({"user_id": "u1"})
    assert not live.complete and live.warnings


def test_malformed_live_items_are_skipped_and_make_the_listing_incomplete(fake):
    fake.get_all = lambda **k: {"results": [
        {"id": "ok", "memory": "fine", "user_id": "u1"}, {"id": "ok", "memory": "dup"},
        {"id": None, "memory": "x"}, {"id": "b", "memory": 5}, "garbage",
    ]}
    live = adapter_for(fake).list_memories({"user_id": "u1"})
    assert [m.id for m in live.memories] == ["ok"] and not live.complete and len(live.warnings) == 4


def test_failures_inside_mem0_become_controlled_errors_without_control_characters(fake):
    def boom(**kw):
        raise RuntimeError("bad\x1b[31m\nthing")
    fake.get_all = boom
    with pytest.raises(AdapterError) as err:
        adapter_for(fake).list_memories({"user_id": "u1"})
    assert "\x1b" not in str(err.value) and "\n" not in str(err.value)
    for shape in (None, [], {"results": "no"}, {"nothing": []}):
        fake.get_all = lambda s=shape, **k: s
        with pytest.raises(AdapterError, match="unexpected shape"):
            adapter_for(fake).list_memories({"user_id": "u1"})


def test_per_memory_history_through_the_public_api(fake):
    fake.api_add("m1", "a", 0)
    fake.api_update("m1", "b", 10)
    events = adapter_for(fake).history("m1")
    assert [e.after for e in events] == ["a", "b"]
    for bad in ("", None, "m" * 300, 7):
        with pytest.raises(AdapterError):
            adapter_for(fake).history(bad)
    fake.history = lambda mid: "nope"
    with pytest.raises(AdapterError):
        adapter_for(fake).history("m1")
    fake.history = lambda mid: ["not a record", {"memory_id": "m1", "event": "ADD", "new_memory": "x", "id": "r"}]
    assert len(adapter_for(fake).history("m1")) == 1


# -- the real Mem0 writer ------------------------------------------------------------------------------

def test_reads_a_history_file_written_by_the_real_mem0_storage_class(tmp_path):
    storage = pytest.importorskip("mem0.memory.storage")
    path = tmp_path / "real.db"
    manager = storage.SQLiteManager(str(path))
    manager.add_history("m1", None, "Works at Acme", "ADD", created_at=iso(0), updated_at=iso(0),
                        actor_id="alice", role="user")
    manager.add_history("m1", "Works at Acme", "Works at Initech", "UPDATE", created_at=iso(0), updated_at=iso(20))
    manager.add_history("m1", "Works at Initech", None, "DELETE", created_at=iso(0), updated_at=iso(40), is_deleted=1)
    manager.close()

    class Stub:
        def get_all(self, **k): return {"results": []}
        def history(self, memory_id): return storage.SQLiteManager(str(path)).get_history(memory_id)

    adapter = Mem0Adapter(Stub(), path, clock=lambda: NOW)
    read = adapter.read_history(10)
    assert [(e.op.value, e.ts) for e in read.events] == [
        ("ADD", BASE), ("UPDATE", BASE + timedelta(minutes=20)), ("DELETE", BASE + timedelta(minutes=40)),
    ]
    assert read.events[0].source.actor_id == "alice" and read.skipped == 0 and not read.warnings
    assert [e.op.value for e in adapter.history("m1")] == ["ADD", "UPDATE", "DELETE"]


# -- telemetry -----------------------------------------------------------------------------------------------

def test_telemetry_is_switched_off_unless_the_user_turned_it_on(monkeypatch):
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    import sys
    import types
    monkeypatch.setitem(sys.modules, "mem0.memory.telemetry", types.SimpleNamespace(MEM0_TELEMETRY=False))
    assert ensure_mem0_telemetry_off() == []
    import os
    assert os.environ["MEM0_TELEMETRY"] == "false"
    monkeypatch.setitem(sys.modules, "mem0.memory.telemetry", types.SimpleNamespace(MEM0_TELEMETRY=True))
    assert "already imported" in ensure_mem0_telemetry_off()[0]
    monkeypatch.setenv("MEM0_TELEMETRY", "true")
    assert "environment" in ensure_mem0_telemetry_off()[0]


def test_read_only_holds_even_if_the_second_lock_is_lifted(fake):
    """Two independent locks: the file is opened read-only AND query_only is on."""
    fake.api_add("m1", "x", 0)
    before = sha(fake.path)
    conn = adapter_for(fake)._connect_ro()
    try:
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO history (id, memory_id) VALUES ('x', 'y')")
    finally:
        conn.close()
    assert sha(fake.path) == before


def test_total_text_is_budgeted_so_a_huge_history_cannot_exhaust_memory(fake):
    for i in range(5):
        raw_row(fake.path, memory_id=f"m{i}", new_memory="x" * 1000)
    read = adapter_for(fake, max_total_chars=2500).read_history(100)
    assert read.truncated and len(read.events) == 2 and any("budget" in w for w in read.warnings)
