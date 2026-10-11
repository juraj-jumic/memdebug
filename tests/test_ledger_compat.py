"""Ledgers written by real earlier releases must still open, verify, report and accept new entries; and the hash format is pinned.

The files in tests/data/ledgers/ are plain SQL dumps of ledgers made by memdebug 0.2.0, 0.3.0, 0.4.1 and 0.6.1 (made-up notes only; see
make_fixture.py there for how they were produced). If one of these tests fails after a change, the change would break people's existing ledgers:
fix the change, not the fixture.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
from memdebug.ledger import GENESIS, SCHEMA_VERSION, Ledger, canonical, compute_hash
from memdebug.models import MemoryEvent, Op

DATA = Path(__file__).parent / "data" / "ledgers"
FIXTURES = sorted(DATA.glob("memdebug-*.sql"))
runner = CliRunner()


def load(tmp_path, fixture):
    """The fixture as a ledger file, and the facts its first line states."""
    sql = fixture.read_text(encoding="utf-8")
    header = re.match(r"-- memdebug (\S+); user_version=(\d+); events=(\d+)", sql)
    assert header, f"{fixture.name} has no header line"
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(sql)
    db.execute(f"PRAGMA user_version = {int(header.group(2))}")
    db.commit()
    db.close()
    return path, header.group(1), int(header.group(2)), int(header.group(3))


def test_the_fixtures_are_there_for_every_release_that_was_tried():
    assert [f.name for f in FIXTURES] == ["memdebug-0.2.0.sql", "memdebug-0.3.0.sql", "memdebug-0.4.1.sql", "memdebug-0.6.1.sql"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.stem.split("-")[1])
def test_a_ledger_from_an_earlier_release_opens_and_verifies_with_every_entry(tmp_path, fixture):
    path, version, stored_version, events = load(tmp_path, fixture)
    assert stored_version == SCHEMA_VERSION  # every published release has written this schema
    ledger = Ledger(path)
    result = ledger.verify()
    assert result.ok, (version, result.problems)
    assert len(ledger.entries()) == events and ledger.counts()["events"] == events
    ledger.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION  # opening changed no version


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.stem.split("-")[1])
def test_the_read_only_open_the_viewer_uses_works_on_it(tmp_path, fixture):
    path, _, _, events = load(tmp_path, fixture)
    ledger = Ledger.open_readonly(path)
    assert ledger.counts()["events"] == events and ledger.verify().ok
    ledger.close()


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.stem.split("-")[1])
def test_new_entries_can_be_added_after_old_ones_and_the_whole_chain_still_verifies(tmp_path, fixture):
    path, _, _, events = load(tmp_path, fixture)
    ledger = Ledger(path)
    before = ledger.head()
    entry = ledger.append(MemoryEvent(backend="folder", memory_id="new.md", op=Op.ADD, ts=datetime(2026, 10, 11, tzinfo=timezone.utc),
                                      scope={"folder": "x"}, after="written by the current version"))
    assert entry.prev_hash == before and ledger.verify().ok and len(ledger.entries()) == events + 1
    ledger.close()


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.stem.split("-")[1])
def test_the_commands_read_an_old_ledger(tmp_path, fixture):
    path, _, _, _ = load(tmp_path, fixture)
    assert runner.invoke(cli.app, ["verify", "--db", str(path)]).exit_code == 0
    report = runner.invoke(cli.app, ["report", "--db", str(path), "--format", "json"])
    assert report.exit_code == 0 and json.loads(report.output)  # a report of an old ledger is still valid JSON


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.stem.split("-")[1])
def test_tampering_with_an_old_ledger_is_still_noticed(tmp_path, fixture):
    path, _, _, _ = load(tmp_path, fixture)
    db = sqlite3.connect(path)
    seq, payload = db.execute("SELECT seq, payload FROM events WHERE payload LIKE '%likes tea%' ORDER BY seq LIMIT 1").fetchone()
    db.execute("UPDATE events SET payload = ? WHERE seq = ?", (payload.replace("likes tea", "likes TEA", 1), seq))
    db.commit()
    db.close()
    result = Ledger(path).verify()
    assert not result.ok and result.problems


@pytest.mark.parametrize("fixture", [f for f in FIXTURES if not f.name.endswith("0.2.0.sql")], ids=lambda f: f.stem.split("-")[1])
def test_snapshots_from_earlier_releases_still_load_and_check(tmp_path, fixture):
    path, _, _, _ = load(tmp_path, fixture)
    ledger = Ledger(path)
    infos = ledger.list_snapshots()
    assert infos and all(ledger.load_snapshot(info.id).memories for info in infos)
    ledger.close()


# -- the hash format is pinned: changing it would orphan every existing ledger ---------------------------------------------

def test_the_chain_hash_is_pinned():
    assert compute_hash(GENESIS, "e1", '{"a":1}') == "a83988bf950f8de62ee35ce3e54513449c81c55853a0336c4306010425e4dc5d"


def test_the_text_that_is_hashed_for_an_event_is_pinned():
    """If a field is added to or renamed in MemoryEvent, this fails on purpose: that changes what new entries hash. Decide it deliberately and say so in the changelog."""
    event = MemoryEvent(backend="folder", memory_id="a.md", op=Op.ADD, ts=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
                        scope={"folder": "x"}, after="likes tea")
    assert canonical(event) == ('{"after":"likes tea","backend":"folder","backend_ref":null,"before":null,"memory_id":"a.md","op":"ADD",'
                                '"scope":{"folder":"x"},"source":null,"trust":"unknown","ts":"2026-10-11T12:00:00Z","ts_observed":false}')


def test_the_stored_operation_names_are_pinned():
    assert [op.value for op in Op] == ["ADD", "UPDATE", "DELETE", "EXTERNAL", "SNAPSHOT", "SNAP_DEL", "ROLLBACK"]
