import logging
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from memdebug.errors import LedgerConflictError, LedgerError
from memdebug.ledger import GENESIS, Ledger
from memdebug.models import MemoryEvent, Op, Source, SourceKind, Trust

T = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def ev(n: int, **kw) -> MemoryEvent:
    return MemoryEvent(backend="fake", memory_id=f"m{n}", op=Op.ADD, ts=T, after=f"text {n}", **kw)


def make(tmp_path, count=4):
    path = tmp_path / "ledger.db"
    ledger = Ledger(path)
    for i in range(1, count + 1):
        ledger.append(ev(i))
    return ledger, path


def test_entries_chain_from_genesis(tmp_path):
    ledger, _ = make(tmp_path)
    entries = ledger.entries()
    assert entries[0].prev_hash == GENESIS
    for earlier, later in zip(entries, entries[1:], strict=False):
        assert later.prev_hash == earlier.hash
    assert ledger.head() == entries[-1].hash
    assert ledger.verify().ok


def test_edited_entry_is_detected(tmp_path):
    ledger, path = make(tmp_path)
    db = sqlite3.connect(path)
    db.execute("UPDATE events SET payload = replace(payload, 'text 2', 'text 9') WHERE seq = 2")
    db.commit()
    assert ledger.verify().problems == ["seq 2 was changed after it was written"]


def test_removed_middle_entry_is_detected(tmp_path):
    ledger, path = make(tmp_path)
    db = sqlite3.connect(path)
    db.execute("DELETE FROM events WHERE seq = 2")
    db.commit()
    result = ledger.verify()
    assert not result.ok and any("missing" in p for p in result.problems)


def test_removed_newest_entries_need_the_expected_head(tmp_path):
    ledger, path = make(tmp_path)
    recorded_head = ledger.head()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM events WHERE seq > 2")
    db.commit()
    assert ledger.verify().ok  # the chain alone cannot see this
    assert not ledger.verify(expected_head=recorded_head).ok


def test_index_columns_cannot_be_changed_unnoticed(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    ledger.append(ev(1, backend_ref="row-1"))
    db = sqlite3.connect(tmp_path / "l.db")
    db.execute("UPDATE events SET ref = 'something-else' WHERE seq = 1")
    db.commit()
    assert any("index columns" in p for p in ledger.verify().problems)


def test_trust_comes_from_source_kind(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    tool = ledger.append(ev(1, source=Source(kind=SourceKind.TOOL_RESULT)))
    user = ledger.append(ev(2, source=Source(kind=SourceKind.USER_MESSAGE)))
    none = ledger.append(ev(3))
    assert (tool.event.trust, user.event.trust, none.event.trust) == (
        Trust.UNTRUSTED, Trust.TRUSTED, Trust.UNKNOWN,
    )


def test_append_many_is_all_or_nothing(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    ledger.append(ev(1, backend_ref="r1"))
    with pytest.raises(LedgerConflictError):
        ledger.append_many([ev(2, backend_ref="r2"), ev(3, backend_ref="r1")])  # r1 repeats
    assert [e.event.memory_id for e in ledger.entries()] == ["m1"]
    assert ledger.verify().ok


def test_same_backend_row_cannot_be_recorded_twice(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    ledger.append(ev(1, backend_ref="r1"))
    with pytest.raises(LedgerConflictError):
        ledger.append(ev(2, backend_ref="r1"))
    assert ledger.known_refs("fake") == {"r1"}


def test_two_writers_on_one_file_keep_one_chain(tmp_path):
    a, b = Ledger(tmp_path / "l.db"), Ledger(tmp_path / "l.db")
    for i in range(3):
        a.append(ev(i))
        b.append(ev(i + 10))
    assert [e.seq for e in a.entries()] == [1, 2, 3, 4, 5, 6]
    assert a.verify().ok


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_new_ledger_is_owner_only(tmp_path):
    Ledger(tmp_path / "l.db")
    assert (tmp_path / "l.db").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_open_permissions_are_reported(tmp_path, caplog):
    path = tmp_path / "l.db"
    Ledger(path).close()
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="memdebug.ledger"):
        Ledger(path)
    assert "readable by other users" in caplog.text


def test_symlinked_ledger_is_refused(tmp_path):
    (tmp_path / "real.db").write_bytes(b"")
    try:
        (tmp_path / "link.db").symlink_to(tmp_path / "real.db")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created here (Windows: enable Developer Mode)")
    with pytest.raises(LedgerError, match="symlink"):
        Ledger(tmp_path / "link.db")


def test_unsuitable_paths_raise_ledger_errors(tmp_path):
    with pytest.raises(LedgerError):
        Ledger(tmp_path)  # a directory
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "missing-dir" / "l.db")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 100)
    with pytest.raises(LedgerError):
        Ledger(junk)
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE history (x)")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError, match="not a memdebug ledger"):
        Ledger(other)


def test_unreadable_entry_raises_a_clear_error_and_verify_still_works(tmp_path):
    ledger, path = make(tmp_path, 2)
    db = sqlite3.connect(path)
    db.execute("UPDATE events SET payload = '{\"nope\": 1}' WHERE seq = 2")
    db.commit()
    with pytest.raises(LedgerError, match="seq 2"):
        ledger.entries()
    assert not ledger.verify().ok
