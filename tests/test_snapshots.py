import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from memdebug import ledger as ledger_module
from memdebug.errors import LedgerError, SnapshotError
from memdebug.ledger import GENESIS, Ledger, canonical, compute_hash, parse_snapshot_id
from memdebug.models import Memory, MemoryEvent, Op
from memdebug.reconcile import replay_events

T = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
U1 = {"user_id": "u1"}


def mem(mid, text, scope=None):
    return Memory(id=mid, text=text, scope=scope or dict(U1))


def ev(n, **kw):
    return MemoryEvent(backend="fake", memory_id=f"m{n}", op=Op.ADD, ts=T, after=f"text {n}", **kw)


def snap(ledger, memories, *, complete=True, label=None, backend="fake", scope=None):
    return ledger.save_snapshot(backend, scope or dict(U1), memories, complete=complete, taken_at=T, label=label)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "l.db")


def raw(ledger):
    return sqlite3.connect(ledger.path)


# -- saving and loading ------------------------------------------------------------------------------

def test_round_trip_keeps_text_exactly(ledger):
    tricky = ["plain", "caf\u00e9 \u65e5\u672c\u8a9e \U0001f600", "tab\tnul\x00esc\x1b[2J\nnewline\r\n", "", " " * 5]
    memories = [mem(f"m{i}", t, {"user_id": "u1", "agent_id": f"a{i}"}) for i, t in enumerate(tricky)]
    info = snap(ledger, memories, label="before the demo")
    loaded = ledger.load_snapshot(info.id)
    assert [(m.id, m.text, m.scope) for m in loaded.memories] == sorted(
        [(m.id, m.text, m.scope) for m in memories])
    assert (loaded.info.count, loaded.info.complete, loaded.info.label) == (5, True, "before the demo")
    assert loaded.info.scope == U1 and loaded.info.backend == "fake"
    assert ledger.verify().ok


def test_snapshot_is_anchored_to_the_ledger_and_chained_into_it(ledger):
    first = snap(ledger, [])
    assert (first.ledger_seq, first.ledger_head) == (0, GENESIS)
    ledger.append(ev(1))
    ledger.append(ev(2))
    second = snap(ledger, [mem("m1", "text 1")])
    assert second.ledger_seq == 3  # after the first snapshot's own event and two memory events
    entry = ledger.entries()[-1]
    assert entry.event.op == Op.SNAPSHOT and entry.event.memory_id == "snapshot:s2"
    assert ledger.verify().ok


def test_ids_count_up_and_are_never_reused(ledger):
    assert snap(ledger, []).id == "s1"
    assert snap(ledger, []).id == "s2"
    ledger.delete_snapshot("s2")
    assert snap(ledger, []).id == "s3"
    assert [i.id for i in ledger.list_snapshots()] == ["s1", "s3"]


def test_identical_texts_are_stored_once(ledger):
    base = [mem("a", "alpha"), mem("b", "beta"), mem("c", "gamma")]
    for _ in range(3):
        snap(ledger, base)
    count = lambda: raw(ledger).execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    assert count() == 3
    snap(ledger, [mem("a", "alpha"), mem("b", "beta changed"), mem("c", "gamma")])
    assert count() == 4


def test_large_snapshots_load_completely(ledger):
    memories = [mem(f"m{i:05d}", f"text {i}") for i in range(2000)]  # more than one query chunk
    info = snap(ledger, memories)
    assert len(ledger.load_snapshot(info.id).memories) == 2000 and ledger.verify().ok


def test_snapshots_may_be_empty_or_incomplete(ledger):
    assert ledger.load_snapshot(snap(ledger, []).id).memories == []
    assert snap(ledger, [mem("a", "x")], complete=False).complete is False


# -- deleting -----------------------------------------------------------------------------------------------

def test_delete_is_chained_and_only_unshared_texts_go(ledger):
    s1 = snap(ledger, [mem("a", "shared"), mem("b", "only in one")])
    s2 = snap(ledger, [mem("a", "shared")])
    ledger.delete_snapshot(s1.id)
    texts = {t for (t,) in raw(ledger).execute("SELECT text FROM blobs")}
    assert texts == {"shared"}
    with pytest.raises(SnapshotError, match="does not exist"):
        ledger.load_snapshot(s1.id)
    assert ledger.load_snapshot(s2.id).memories[0].text == "shared"
    assert ledger.entries()[-1].event.op == Op.SNAPSHOT_DELETED and ledger.verify().ok
    with pytest.raises(SnapshotError):
        ledger.delete_snapshot(s1.id)


def test_a_snapshot_removed_behind_the_ledgers_back_is_reported(ledger):
    info = snap(ledger, [mem("a", "x")])
    conn = raw(ledger)
    conn.execute("DELETE FROM snapshot_entries")
    conn.execute("DELETE FROM snapshots")
    conn.commit()
    assert any(f"snapshot {info.id} is recorded in the ledger but is missing" in p for p in ledger.verify().problems)


# -- tampering ------------------------------------------------------------------------------------------------

def test_a_changed_stored_text_is_caught_everywhere(ledger):
    info = snap(ledger, [mem("a", "the original text")])
    conn = raw(ledger)
    conn.execute("UPDATE blobs SET text = 'forged text'")
    conn.commit()
    with pytest.raises(SnapshotError, match="changed"):
        ledger.load_snapshot(info.id)
    assert any("does not match its hash" in p for p in ledger.verify().problems)


@pytest.mark.parametrize("sql", [
    "UPDATE snapshot_entries SET hash = '" + "0" * 64 + "'",
    "UPDATE snapshot_entries SET memory_id = 'other' WHERE memory_id = 'a'",
    "UPDATE snapshot_entries SET scope = '{\"user_id\":\"u2\"}'",
    "UPDATE snapshots SET complete = 0",
    "UPDATE snapshots SET label = 'forged'",
    "UPDATE snapshots SET backend = 'other'",
    "UPDATE snapshots SET scope = '{\"user_id\":\"u2\"}'",
    "UPDATE snapshots SET taken_at = '2020-01-01T00:00:00+00:00'",
    "UPDATE snapshots SET ledger_head = '" + "1" * 64 + "'",
    "UPDATE snapshots SET ledger_seq = 5",
    "UPDATE snapshots SET count = 99",
    "UPDATE snapshots SET entries_hash = '" + "2" * 64 + "'",
    "DELETE FROM snapshot_entries",
])
def test_every_stored_field_of_a_snapshot_is_protected(ledger, sql):
    ledger.append(ev(1))
    info = snap(ledger, [mem("a", "x"), mem("b", "y")], label="label")
    conn = raw(ledger)
    conn.execute(sql)
    conn.commit()
    with pytest.raises(SnapshotError):
        ledger.load_snapshot(info.id)
    assert not ledger.verify().ok


def test_a_rewritten_snapshot_with_a_recomputed_hash_still_disagrees_with_the_chain(ledger):
    """An attacker who also fixes the snapshot's own hash is still caught by the chain record."""
    info = snap(ledger, [mem("a", "x")])
    conn = raw(ledger)
    new_hash = ledger_module.snapshot_digest(
        1, "fake", U1, T.isoformat(), info.ledger_seq, info.ledger_head, True, None,
        [["a", ledger_module._text_digest("forged"), U1]])
    conn.execute("INSERT OR IGNORE INTO blobs VALUES (?, 'forged')", (ledger_module._text_digest("forged"),))
    conn.execute("UPDATE snapshot_entries SET hash = ?", (ledger_module._text_digest("forged"),))
    conn.execute("UPDATE snapshots SET entries_hash = ?", (new_hash,))
    conn.commit()
    with pytest.raises(SnapshotError, match="ledger chain"):
        ledger.load_snapshot(info.id)
    assert any("does not match its record in the ledger chain" in p for p in ledger.verify().problems)


def test_a_removed_blob_is_reported(ledger):
    info = snap(ledger, [mem("a", "x")])
    conn = raw(ledger)
    conn.execute("DELETE FROM blobs")
    conn.commit()
    with pytest.raises(SnapshotError, match="missing"):
        ledger.load_snapshot(info.id)
    assert any("missing" in p for p in ledger.verify().problems)


def test_truncating_the_newest_ledger_entries_is_caught_by_the_snapshot_anchor(ledger):
    for i in range(3):
        ledger.append(ev(i))
    info = snap(ledger, [mem("m1", "text 1")])
    assert ledger.verify().ok
    conn = raw(ledger)
    conn.execute("DELETE FROM events WHERE seq >= ?", (info.ledger_seq,))  # drop the anchor and everything after
    conn.commit()
    problems = ledger.verify().problems
    assert any("no longer match" in p for p in problems) and any("not recorded in the ledger chain" in p for p in problems)


def test_a_deleted_snapshot_whose_data_was_restored_is_reported(ledger):
    info = snap(ledger, [mem("a", "x")])
    conn = raw(ledger)
    saved = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("snapshots", "snapshot_entries", "blobs")}
    ledger.delete_snapshot(info.id)
    for table, rows in saved.items():
        for row in rows:
            conn.execute(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' * len(row))})", row)
    conn.commit()
    assert any("was deleted in the ledger but still exists" in p for p in ledger.verify().problems)


# -- limits, validation and atomicity ------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["", "x" * 101, "new\nline", "esc\x1b[2J", "bidi\u202e"])
def test_bad_labels_are_refused(ledger, label):
    with pytest.raises(SnapshotError):
        snap(ledger, [], label=label)
    assert ledger.list_snapshots() == [] and ledger.entries() == []


def test_other_bad_input_is_refused_and_nothing_is_written(ledger, monkeypatch):
    with pytest.raises(SnapshotError, match="same id twice"):
        snap(ledger, [mem("a", "x"), mem("a", "y")])
    with pytest.raises(SnapshotError, match="timezone"):
        ledger.save_snapshot("fake", U1, [], complete=True, taken_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):  # the model already refuses text that is not valid Unicode ...
        mem("a", "lone \ud800 surrogate")
    with pytest.raises(SnapshotError, match="not valid Unicode"):  # ... and the ledger refuses it as well
        ledger_module._text_digest("lone \ud800 surrogate")
    monkeypatch.setattr(ledger_module, "MAX_SNAPSHOT_MEMORIES", 2)
    with pytest.raises(SnapshotError, match="too many"):
        snap(ledger, [mem(str(i), "x") for i in range(3)])
    monkeypatch.setattr(ledger_module, "MAX_SNAPSHOT_CHARS", 10)
    with pytest.raises(SnapshotError, match="too large"):
        snap(ledger, [mem("a", "x" * 11)])
    assert ledger.list_snapshots() == [] and ledger.entries() == []
    assert raw(ledger).execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_a_failure_while_saving_leaves_no_trace(ledger, monkeypatch):
    def boom(events):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(ledger, "_append_locked", boom)
    with pytest.raises(SnapshotError):
        snap(ledger, [mem("a", "x")])
    monkeypatch.undo()
    assert ledger.list_snapshots() == [] and raw(ledger).execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    assert snap(ledger, [mem("a", "x")]).id == "s1" and ledger.verify().ok  # even the id counter rolled back


@pytest.mark.parametrize("value, ok", [
    ("s1", True), ("s12", True), ("s999999999", True),
    ("S1", False), ("1", False), ("s0", False), ("s01", False), ("s", False), ("s1 ", False), ("s1;DROP", False),
    ("s1'--", False), ("s9999999999", False), ("", False), (None, False), (5, False), ("s-1", False),
])
def test_snapshot_ids_are_validated(value, ok):
    if ok:
        assert parse_snapshot_id(value) == int(value[1:])
    else:
        with pytest.raises(SnapshotError):
            parse_snapshot_id(value)


def test_a_missing_snapshot_is_reported_clearly(ledger):
    with pytest.raises(SnapshotError, match="does not exist"):
        ledger.load_snapshot("s5")


# -- bookkeeping must not disturb anything else ------------------------------------------------------------------------

def test_bookkeeping_events_cannot_be_forged_through_append(ledger):
    forged = MemoryEvent(backend="fake", memory_id="snapshot:s1", op=Op.SNAPSHOT, ts=T, after="{}")
    with pytest.raises(LedgerError, match="snapshot methods"):
        ledger.append(forged)
    assert ledger.entries() == []


def test_bookkeeping_events_are_not_memory_state(ledger):
    ledger.append(ev(1))
    snap(ledger, [mem("m1", "text 1")])
    # checked while the snapshot exists: a later deletion event would otherwise hide a phantom memory
    assert replay_events(e.event for e in ledger.entries()).keys() == {"m1"}
    ledger.delete_snapshot("s1")
    assert replay_events(e.event for e in ledger.entries()).keys() == {"m1"}


def test_a_backend_row_id_that_looks_like_a_snapshot_cannot_collide(ledger):
    ledger.append(ev(1, backend_ref="SNAPSHOT:s1"))
    ledger.append(ev(2, backend_ref="SNAPSHOT:snapshot:s1"))
    info = snap(ledger, [])
    assert info.id == "s1" and ledger.verify().ok
    assert ledger.known_refs("fake") == {"SNAPSHOT:s1", "SNAPSHOT:snapshot:s1"}


def test_two_writers_taking_snapshots_keep_one_valid_chain(tmp_path):
    a, b = Ledger(tmp_path / "l.db"), Ledger(tmp_path / "l.db")
    ids = [snap(w, [mem("a", f"v{i}")]).id for i, w in enumerate([a, b, a, b])]
    assert ids == ["s1", "s2", "s3", "s4"] and a.verify().ok and b.verify().ok
    a.append(ev(1))
    assert b.load_snapshot("s3").memories[0].text == "v2"


# -- upgrading an older ledger ---------------------------------------------------------------------------------------------

def make_v1_ledger(path, count=3):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, backend TEXT NOT NULL, "
                 "ref TEXT, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)")
    conn.execute("CREATE UNIQUE INDEX idx_events_ref ON events(backend, ref) WHERE ref IS NOT NULL")
    prev = GENESIS
    for seq in range(1, count + 1):
        event = ev(seq, backend_ref=f"r{seq}")
        payload, digest = canonical(event), compute_hash(prev, f"e{seq}", canonical(event))
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (seq, f"e{seq}", "fake", f"r{seq}", payload, prev, digest))
        prev = digest
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return prev


def test_a_version_1_ledger_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    old_head = make_v1_ledger(path)
    ledger = Ledger(path)
    assert ledger.head() == old_head and len(ledger.entries()) == 3 and ledger.verify().ok
    assert ledger.known_refs("fake") == {"r1", "r2", "r3"}
    info = snap(ledger, [mem("a", "x")])
    assert info.ledger_seq == 3 and ledger.verify().ok
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 3
    Ledger(path)  # opening again is fine


def test_unknown_ledger_versions_are_refused(tmp_path):
    path = tmp_path / "future.db"
    Ledger(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError, match="supported version"):
        Ledger(path)


def make_v2_ledger(path):
    """A version-2 ledger: snapshot support, but no op/trust columns."""
    make_v1_ledger(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE events ADD COLUMN snap TEXT")
    conn.execute("CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL")
    conn.execute("CREATE TABLE blobs (hash TEXT PRIMARY KEY, text TEXT NOT NULL)")
    conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL, "
                 "scope TEXT NOT NULL, taken_at TEXT NOT NULL, ledger_seq INTEGER NOT NULL, ledger_head TEXT NOT NULL, "
                 "complete INTEGER NOT NULL, label TEXT, count INTEGER NOT NULL, entries_hash TEXT NOT NULL)")
    conn.execute("CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL, memory_id TEXT NOT NULL, "
                 "hash TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY (snapshot_id, memory_id))")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def test_a_version_2_ledger_gains_the_filter_columns_and_keeps_its_chain(tmp_path):
    path = tmp_path / "v2.db"
    make_v2_ledger(path)
    ledger = Ledger(path)
    rows = sqlite3.connect(path).execute("SELECT op, trust FROM events ORDER BY seq").fetchall()
    assert rows == [("ADD", "unknown")] * 3
    assert ledger.verify().ok
    assert ledger.counts()["by_op"] == {"ADD": 3}


def test_upgrading_never_changes_a_single_event_byte(tmp_path):
    path = tmp_path / "v1.db"
    make_v1_ledger(path)
    before = sqlite3.connect(path).execute("SELECT seq, id, payload, prev_hash, hash FROM events").fetchall()
    Ledger(path).close()
    after = sqlite3.connect(path).execute("SELECT seq, id, payload, prev_hash, hash FROM events").fetchall()
    assert before == after


@pytest.mark.parametrize("column, value", [("op", "DELETE"), ("trust", "trusted"), ("op", None)])
def test_the_filter_columns_cannot_be_changed_unnoticed(ledger, column, value):
    ledger.append(ev(1))
    conn = raw(ledger)
    conn.execute(f"UPDATE events SET {column} = ? WHERE seq = 1", (value,))
    conn.commit()
    assert any("index columns" in p for p in ledger.verify().problems)
