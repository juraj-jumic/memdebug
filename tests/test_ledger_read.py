import hashlib
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from memdebug.errors import LedgerError
from memdebug.ledger import Ledger
from memdebug.models import Memory, MemoryEvent, Op, Source, SourceKind
from test_snapshots import make_v1_ledger, make_v2_ledger

T = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def ev(n, op=Op.ADD, **kw):
    return MemoryEvent(backend="fake", memory_id=f"m{n}", op=op, ts=T, after=f"text {n}", **kw)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture
def filled(tmp_path):
    path = tmp_path / "l.db"
    ledger = Ledger(path)
    for n in range(1, 8):
        ledger.append(ev(n))
    ledger.append(ev(8, Op.EXTERNAL))
    ledger.append(ev(9, source=Source(kind=SourceKind.TOOL_RESULT)))
    ledger.save_snapshot("fake", {"user_id": "u1"}, [Memory(id="m1", text="x")], complete=True, taken_at=T)
    ledger.close()
    return path


# -- read-only access ---------------------------------------------------------------------------------

def test_a_read_only_ledger_reads_everything_and_changes_nothing(filled):
    before = sha(filled)
    ro = Ledger.open_readonly(filled)
    assert len(ro.entries()) == 10 and ro.verify().ok and ro.list_snapshots()[0].id == "s1"
    assert ro.load_snapshot("s1").memories[0].text == "x"
    ro.close()
    assert sha(filled) == before


def test_every_write_is_refused_on_a_read_only_ledger(filled):
    ro = Ledger.open_readonly(filled)
    with pytest.raises(LedgerError, match="read-only"):
        ro.append(ev(99))
    with pytest.raises(LedgerError, match="read-only"):
        ro.append_many([ev(98)])
    with pytest.raises(LedgerError, match="read-only"):
        ro.save_snapshot("fake", {}, [], complete=True, taken_at=T)
    with pytest.raises(LedgerError, match="read-only"):
        ro.delete_snapshot("s1")
    # and even raw SQL through its own connection cannot write
    for sql in ("DELETE FROM events", "UPDATE events SET op = 'X'", "DROP TABLE events", "PRAGMA user_version = 9"):
        with pytest.raises(sqlite3.Error):
            ro._db.execute(sql)
    assert len(Ledger.open_readonly(filled).entries()) == 10


def test_opening_read_only_never_creates_or_upgrades_a_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(LedgerError):
        Ledger.open_readonly(missing)
    assert not missing.exists()
    for maker, name in ((make_v1_ledger, "v1.db"), (make_v2_ledger, "v2.db")):
        path = tmp_path / name
        maker(path)
        before = sha(path)
        with pytest.raises(LedgerError, match="older version"):
            Ledger.open_readonly(path)
        assert sha(path) == before  # still version 1 / 2: nothing was upgraded
    with pytest.raises(LedgerError):
        Ledger.open_readonly(tmp_path)  # a directory
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE history (x)")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError, match="not a memdebug ledger"):
        Ledger.open_readonly(other)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(LedgerError):
        Ledger.open_readonly(junk)


def test_a_symlinked_ledger_is_refused_for_reading(filled, tmp_path):
    link = tmp_path / "link.db"
    try:
        link.symlink_to(filled)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created here")
    with pytest.raises(LedgerError, match="plain regular file"):
        Ledger.open_readonly(link)


def test_a_reader_does_not_block_a_writer_and_sees_new_events(filled):
    ro = Ledger.open_readonly(filled)
    writer = Ledger(filled)
    writer.append(ev(50))
    assert ro.counts()["events"] == 11 and ro.verify().ok


def test_a_locked_ledger_fails_fast_instead_of_hanging(filled):
    blocker = sqlite3.connect(filled, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(LedgerError):
            Ledger.open_readonly(filled, busy_timeout=0.3).counts()
        assert time.monotonic() - started < 4
    finally:
        blocker.execute("ROLLBACK")


# -- counts, pages and single events ----------------------------------------------------------------------------

def test_counts(filled):
    counts = Ledger.open_readonly(filled).counts()
    assert counts["events"] == 10 and counts["snapshots"] == 1 and counts["untrusted"] == 1
    assert counts["by_op"] == {"ADD": 8, "EXTERNAL": 1, "SNAPSHOT": 1} and counts["head_seq"] == 10
    assert counts["head"] == Ledger.open_readonly(filled).head()


def test_pages_are_newest_first_and_walk_the_whole_ledger(filled):
    ro = Ledger.open_readonly(filled)
    seen, before = [], None
    while True:
        page = ro.events_page(before_seq=before, limit=4)
        if not page:
            break
        seen += [e.seq for e in page]
        before = page[-1].seq
    assert seen == [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]


def test_filters_are_exact(filled):
    ro = Ledger.open_readonly(filled)
    assert [e.id for e in ro.events_page(op="EXTERNAL")] == ["e8"]
    assert [e.id for e in ro.events_page(trust="untrusted")] == ["e9"]
    assert [e.id for e in ro.events_page(op="ADD", before_seq=5)] == ["e4", "e3", "e2", "e1"]
    assert [e.event.op.value for e in ro.events_page(op="SNAPSHOT")] == ["SNAPSHOT"]
    assert ro.events_page(op="DELETE") == []


def test_text_that_looks_like_a_filter_value_does_not_fool_the_filter(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    ledger.append(MemoryEvent(backend="b", memory_id="m", op=Op.ADD, ts=T, after='"op":"EXTERNAL" "trust":"untrusted"'))
    assert ledger.events_page(op="EXTERNAL") == [] and ledger.events_page(trust="untrusted") == []


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 501}, {"limit": "5"}, {"before_seq": 0}, {"before_seq": "1"}, {"before_seq": -3},
    {"op": "ADD' OR '1'='1"}, {"op": "add"}, {"op": ""}, {"trust": "trusted; DROP TABLE events"}, {"trust": "x"},
])
def test_bad_page_arguments_are_refused(filled, kwargs):
    with pytest.raises(LedgerError):
        Ledger.open_readonly(filled).events_page(**kwargs)


def test_single_events_are_found_by_a_validated_id(filled):
    ro = Ledger.open_readonly(filled)
    assert ro.get_entry("e3").event.memory_id == "m3" and ro.get_entry("e999") is None
    for bad in ["e0", "e01", "E1", "1", "e", "e1;DROP", "e1 ", "e-1", "e" + "9" * 13, "", None, 5]:
        with pytest.raises(LedgerError):
            ro.get_entry(bad)


def test_the_file_is_opened_read_only_independently_of_the_query_only_setting(filled):
    """Two locks: SQLite's read-only open and PRAGMA query_only. Lifting the second must not matter."""
    before = sha(filled)
    ro = Ledger.open_readonly(filled)
    ro._db.execute("PRAGMA query_only = OFF")
    for sql in ("DELETE FROM events", "UPDATE events SET op = 'X'", "INSERT INTO blobs VALUES ('a', 'b')"):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro._db.execute(sql)
    assert sha(filled) == before
