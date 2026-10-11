"""The record of the latest check (status.json): what `check` writes, what the viewer shows, and that a hostile file cannot hurt the page."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.laststatus as ls
import memdebug.monitor as mon
import memdebug.stores as st
from memdebug.ledger import Ledger
from test_check_source_lines import add_memory, watched
from test_provenance_openwebui import BASE, make_db
from test_stores_and_monitor import notes_folder, registry_for
from test_viewer import XSS, Viewer, make_ledger, start

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
runner = CliRunner()


def record(**changes):
    base = {"version": 1, "checked_at": NOW.isoformat(), "ledger_ok": True, "ledger_problem": None, "note": None,
            "stores": [{"name": "notes", "kind": "folder", "state": "quiet", "headline": "notes: quiet, nothing new", "lines": [], "memories": 3}]}
    base.update(changes)
    return base


def saved(tmp_path, data):
    path = tmp_path / "status.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


# -- writing and reading ------------------------------------------------------------------------------------------------

def test_a_record_survives_a_write_and_a_read(tmp_path):
    path = ls.status_path(tmp_path / "ledger.db")
    assert path.name == "status.json" and path.parent == tmp_path
    assert ls.write(path, record()) is True
    got = ls.read(path)
    assert got.checked_at == NOW and got.ledger_ok and got.stores[0].name == "notes" and got.stores[0].memories == 3 and not got.needs_a_look
    assert not list(tmp_path.glob("*.tmp"))  # the temporary file is gone


def test_nothing_recorded_yet_reads_as_none(tmp_path):
    assert ls.read(tmp_path / "status.json") is None


def test_a_write_that_cannot_happen_returns_false_and_never_raises(tmp_path):
    assert ls.write(tmp_path / "missing-folder" / "status.json", record()) is False


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_it_never_writes_through_a_link(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / "status.json").symlink_to(target)
    assert ls.write(tmp_path / "status.json", record()) is False and target.read_text(encoding="utf-8") == "keep"
    with pytest.raises(ls.StatusError):
        ls.read(tmp_path / "status.json")


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_a_link_to_a_perfectly_good_record_is_still_not_read(tmp_path):
    good = saved(tmp_path, record())
    link = tmp_path / "linked-status.json"
    link.symlink_to(good)
    with pytest.raises(ls.StatusError, match="not usable"):  # refused because it is a link, not because of what is in it
        ls.read(link)


@pytest.mark.parametrize("bad", ["not json", "[]", "{}", json.dumps(record(version=2)), json.dumps(record(checked_at="yesterday")),
                                 json.dumps(record(checked_at="2026-10-11T12:00:00")), json.dumps(record(stores="x")),
                                 json.dumps(record(stores=["x"]))], ids=["text", "list", "empty", "version", "time", "naive", "stores", "store"])
def test_a_file_of_the_wrong_shape_is_refused_not_guessed_at(tmp_path, bad):
    with pytest.raises(ls.StatusError):
        ls.read(saved(tmp_path, bad))


def test_a_huge_file_is_refused_without_being_read(tmp_path):
    path = saved(tmp_path, " " * (ls.MAX_BYTES + 10))
    with pytest.raises(ls.StatusError, match="not usable"):  # refused by its size, before any parsing
        ls.read(path)


def test_hostile_text_and_values_are_cleaned_when_read_back(tmp_path):
    hostile = record(stores=[{"name": "\x1b[31m" + "n" * 500, "kind": "\x07folder", "state": "<script>", "headline": "a\r\nb‮" + "h" * 5000,
                              "lines": ["x\x1b[2J" * 3] * 50 + [5, None], "memories": True}], note="n\x1b" * 400, ledger_problem="p" * 5000)
    got = ls.read(saved(tmp_path, hostile))
    store = got.stores[0]
    assert "\x1b" not in store.name + store.kind + store.headline + "".join(store.lines) + (got.note or "") + (got.ledger_problem or "")
    assert len(store.name) <= 45 and len(store.headline) <= 405 and len(store.lines) == ls.MAX_LINES
    assert store.state == "error" and store.memories is None  # an unknown state is never shown as fine; True is not a number
    assert got.needs_a_look


@pytest.mark.parametrize("state, needs", [("quiet", False), ("changes", False), ("attention", True), ("incomplete", True), ("error", True)])
def test_which_states_need_a_look(tmp_path, state, needs):
    stores = [{"name": "s", "kind": "folder", "state": state, "headline": "h", "lines": [], "memories": 1}]
    assert ls.read(saved(tmp_path, record(stores=stores))).needs_a_look is needs
    assert ls.read(saved(tmp_path, record(stores=stores, ledger_ok=False))).needs_a_look is True  # a failed ledger always does


# -- what check writes ----------------------------------------------------------------------------------------------------------

def test_check_leaves_the_record_next_to_the_ledger_and_says_the_same_words(tmp_path):
    path, ledger, cfg = watched(tmp_path, chats=[("assistant", BASE + 12)])
    add_memory(path, "m2", "likes coffee")
    reg = registry_for(tmp_path, cfg)
    summary = mon.check_all(reg, ledger, settle=0, ledger_path=tmp_path / "l.db")
    got = ls.read(tmp_path / "status.json")
    assert got.stores[0].state == "changes" and got.stores[0].memories == 2 and got.ledger_ok
    assert got.stores[0].headline == mon.describe(summary.results[0]).strip()
    assert any("where it came from" in line for line in got.stores[0].lines)


def test_without_a_ledger_path_nothing_is_written(tmp_path):
    path, ledger, cfg = watched(tmp_path)
    mon.check_all(registry_for(tmp_path, cfg), ledger, settle=0)
    assert not (tmp_path / "status.json").exists()


def test_a_store_not_read_in_full_is_recorded_as_such(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    result = mon.StoreResult(cfg)
    result.incomplete = True
    summary = mon.CheckSummary([result], True)
    data = mon.check_record(summary, NOW)
    assert data["stores"][0]["state"] == "incomplete" and "NOT READ IN FULL" in data["stores"][0]["headline"]
    assert mon.store_state(mon.StoreResult(cfg)) == "quiet"
    failed = mon.StoreResult(cfg)
    failed.error = "boom"
    assert mon.store_state(failed) == "error"
    outside = mon.StoreResult(cfg)
    outside.outside_history = 1
    assert mon.store_state(outside) == "attention"


def test_a_failing_write_does_not_stop_a_check(tmp_path, monkeypatch):
    path, ledger, cfg = watched(tmp_path)
    monkeypatch.setattr(ls, "write", lambda *a, **k: False)
    summary = mon.check_all(registry_for(tmp_path, cfg), ledger, settle=0, ledger_path=tmp_path / "l.db")
    assert summary.results and summary.ledger_ok


def test_the_check_command_writes_it(tmp_path):
    path = make_db(tmp_path / "w.db", chats=[("user", BASE + 560)])
    db = tmp_path / "l.db"
    assert runner.invoke(cli.app, ["add", str(path), "--db", str(db)]).exit_code == 0
    assert runner.invoke(cli.app, ["check", "--db", str(db), "--settle", "0"]).exit_code == 0
    assert ls.read(tmp_path / "status.json").stores[0].state == "quiet"


# -- the viewer -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def served(tmp_path):
    path = make_ledger(tmp_path)
    server, thread = start(path)
    yield Viewer(server, path), tmp_path
    server.shutdown()
    server.server_close()
    thread.join(5)


def fresh(**changes):
    return record(checked_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(), **changes)


def test_the_status_page_says_so_when_no_check_has_been_recorded(served):
    viewer, _ = served
    status, _, body = viewer.get("/status")
    assert status == 200 and "No check has been recorded yet" in body and "memdebug check" in body


def test_the_status_page_shows_the_latest_check_and_when_it_was_made(served):
    viewer, tmp = served
    saved(tmp, fresh())
    body = viewer.get("/status")[2]
    assert "The latest check found nothing that needs a look" in body and "minutes ago" in body and "notes" in body and "3 memories" in body


def test_a_store_not_read_in_full_is_shown_loudly_on_the_status_page_and_the_front_page(served):
    viewer, tmp = served
    stores = [{"name": "webui", "kind": "openwebui", "state": "incomplete", "headline": "webui: NOT READ IN FULL: nothing new in what could be read",
               "lines": ["where it came from: x"], "memories": 5}]
    saved(tmp, fresh(stores=stores))
    page = viewer.get("/status")[2]
    assert "NOT READ IN FULL" in page and "The latest check says" in page and "where it came from: x" in page
    assert "See the Status page" not in page  # no link to itself
    front = viewer.get("/")[2]
    assert "The latest check says" in front and "webui: NOT READ IN FULL" in front and 'href="/status"' in front


def test_a_failed_ledger_check_and_an_old_record_are_both_said_plainly(served):
    viewer, tmp = served
    old = record(checked_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), ledger_ok=False, ledger_problem="entry 3 was changed")
    saved(tmp, old)
    body = viewer.get("/status")[2]
    assert "This record is old" in body and "entry 3 was changed" in body and "3 days ago" in body


def test_a_quiet_record_adds_no_warning_to_the_front_page(served):
    viewer, tmp = served
    saved(tmp, fresh())
    front = viewer.get("/")[2]
    assert "The latest check says" not in front and "The latest check needs a look" not in front


def test_the_front_page_headline_never_says_nothing_needs_a_look_beside_a_warning(tmp_path):
    from memdebug.ledger import Ledger
    from memdebug.viewer import pages
    quiet = Ledger(tmp_path / "q.db")
    ctx = pages.Context(ledger_name="q.db")
    unread = [{"name": "n", "kind": "folder", "state": "incomplete", "headline": "n: NOT READ IN FULL", "lines": [], "memories": 1}]
    ok = ls.read(saved(tmp_path, record()))
    needs = ls.read(saved(tmp_path, record(stores=unread)))
    assert "Nothing is recorded yet" in pages.overview(quiet, ctx, needs).html  # an empty ledger keeps its own headline
    from memdebug.models import MemoryEvent, Op
    quiet.append(MemoryEvent(backend="folder", memory_id="a.md", op=Op.ADD, ts=NOW, scope={"store": "n"}, after="x"))
    assert "Nothing needs a look." in pages.overview(quiet, ctx, ok).html
    page = pages.overview(quiet, ctx, needs).html
    assert "The latest check needs a look." in page and "Nothing needs a look." not in page and "NOT READ IN FULL" in page


def test_a_broken_record_is_reported_without_breaking_the_pages(served):
    viewer, tmp = served
    saved(tmp, "{broken")
    status, _, body = viewer.get("/status")
    assert status == 200 and "cannot be read" in body and "memdebug check" in body
    assert viewer.get("/")[0] == 200  # the front page just leaves the banner out


def test_hostile_text_in_the_record_is_escaped_on_both_pages(served):
    viewer, tmp = served
    stores = [{"name": XSS, "kind": XSS, "state": "error", "headline": XSS, "lines": [XSS], "memories": 1}]
    saved(tmp, fresh(stores=stores, note=XSS, ledger_ok=False, ledger_problem=XSS))
    for target in ("/status", "/"):
        body = viewer.get(target)[2]
        assert "<script" not in body.lower() and "<img" not in body.lower() and "&lt;script" in body


def test_the_status_page_has_no_form_script_or_link_that_changes_anything(served):
    viewer, tmp = served
    saved(tmp, fresh())
    body = viewer.get("/status")[2]
    assert "<form" not in body and "<script" not in body.lower()
    assert viewer.request("POST", "/status")[0] == 405


def test_the_navigation_lists_the_status_page(served):
    viewer, _ = served
    assert 'href="/status"' in viewer.get("/")[2]
