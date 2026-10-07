"""Opaque ids are shown in a readable way, and the overview never claims a history that a store does not have."""
import html
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.monitor as mon
import memdebug.stores as st
from memdebug.backends import HISTORYLESS, friendly_id, is_opaque_id, short_id
from memdebug.ledger import Ledger
from memdebug.models import MemoryEvent, Op
from memdebug.textsafe import has_unsafe_chars
from payloads import PAYLOADS
from test_viewer import Viewer, start

runner = CliRunner()
T = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
UUID = "3f2a9c1e-7b4d-4e8a-9c56-1d2e3f4a5b6c"


@pytest.mark.parametrize("memory_id", [UUID, UUID.upper(), UUID.replace("-", ""), "a" * 40, "b" * 64])
def test_random_identifiers_are_recognised(memory_id):
    assert is_opaque_id(memory_id) and short_id(memory_id) == memory_id[:8] + "…"


@pytest.mark.parametrize("memory_id", ["prefs.md", "notes/a.md", "m1", "abcdef12", "e1", "snapshot:s1", UUID + "x", UUID + "\n", "g" * 32, ""])
def test_meaningful_or_unusual_ids_are_left_alone(memory_id):
    assert not is_opaque_id(memory_id) and short_id(memory_id) == memory_id


def test_a_label_for_an_opaque_id_includes_what_the_memory_says():
    label = friendly_id(UUID, "Greet me with \"Hi\" when I start a new chat, every single time please", 24)
    assert label.startswith('3f2a9c1e… "Greet me with "Hi" when') and label.endswith('"') and "\n" not in label
    assert friendly_id(UUID, "") == "3f2a9c1e…" and friendly_id(UUID, None) == "3f2a9c1e…"
    assert friendly_id("prefs.md", "anything") == "prefs.md"


@pytest.mark.parametrize("payload", PAYLOADS, ids=[f"p{i}" for i in range(len(PAYLOADS))])
def test_a_label_is_always_safe_to_print(payload):
    assert not has_unsafe_chars(friendly_id(UUID, payload)) and not has_unsafe_chars(friendly_id(payload, payload))


# -- the overview ---------------------------------------------------------------------------------------------------------

def make(path, backends):
    ledger = Ledger(path)
    for i, backend in enumerate(backends):
        ledger.append(MemoryEvent(backend=backend, memory_id=UUID if backend in HISTORYLESS else f"n{i}.md", op=Op.ADD, ts=T, scope={"s": backend},
                                  after="Greet me with Hi when I start a chat.", ts_observed=True))
    ledger.close()


def overview_text(tmp_path, backends):
    path = tmp_path / ("-".join(backends) + ".db")
    make(path, backends)
    server, thread = start(path)
    try:
        return html.unescape(Viewer(server, path).get("/")[2])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_the_overview_never_says_a_change_went_through_a_history_that_does_not_exist(tmp_path):
    only = overview_text(tmp_path, ["openwebui"])
    assert "keep no history of their own" in only and "Every change went through" not in only and "cannot say who" in only
    folder = overview_text(tmp_path, ["folder"])
    assert "keep no history of their own" in folder and "Every change went through" not in folder


def test_with_a_history_the_reassurance_is_kept_and_a_mix_says_which_is_which(tmp_path):
    assert "Every change went through the store's own history." in overview_text(tmp_path, ["markdown-git"])
    mixed = overview_text(tmp_path, ["markdown-git", "openwebui"])
    assert "Changes in the stores that have a history went through it" in mixed and "openwebui keep no history" in mixed and "Every change went through the store" not in mixed


def test_the_overview_points_to_the_witness(tmp_path):
    assert "memdebug witness --file" in overview_text(tmp_path, ["markdown-git"])


def test_rows_show_a_short_id_and_the_detail_view_keeps_the_full_one(tmp_path):
    path = tmp_path / "ids.db"
    make(path, ["openwebui", "markdown-git"])
    server, thread = start(path)
    try:
        viewer = Viewer(server, path)
        listing = viewer.get("/timeline")[2]
        assert "3f2a9c1e…" in listing and UUID not in listing and "n1.md" in listing
        detail = viewer.get("/timeline?event=e1")[2]
        assert UUID in detail and "Memory id" in detail
        assert "Memory id" not in viewer.get("/timeline?event=e2")[2]  # a file name is already meaningful: no extra row
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# -- the command line -------------------------------------------------------------------------------------------------------

def test_check_describes_a_change_to_an_opaque_id_by_what_it_says(tmp_path):
    result = mon.StoreResult(st.StoreConfig("webui", "openwebui", "/x"))
    result.changes = [(Op.ADD, UUID), (Op.DELETE, "gone.md")]
    result.previews = {UUID: "Greet me with Hi when I start a chat", "gone.md": "x"}
    line = mon.describe(result)
    assert 'added: 3f2a9c1e… "Greet me with Hi when I start a' in line and "removed: gone.md" in line and UUID not in line
    result.hints = [(UUID, mon.Hint("send-data", "warning", "asks to send something to an address", "send to a@b.invalid"))]
    assert 'worth a second look: 3f2a9c1e… "Greet me with Hi' in mon.hint_lines(result)[0]


def test_a_removed_opaque_memory_is_labelled_by_the_text_it_had(tmp_path):
    folder_db = tmp_path / "w.db"
    import sqlite3
    db = sqlite3.connect(folder_db)
    db.execute("CREATE TABLE memory (id VARCHAR, user_id VARCHAR, content TEXT, updated_at BIGINT, created_at BIGINT)")
    db.execute("INSERT INTO memory VALUES (?, 'u', 'Greet me with Hi', 1, 1)", (UUID,))
    db.commit()
    db.close()
    ledger_db = tmp_path / "l.db"
    assert runner.invoke(cli.app, ["add", str(folder_db), "--db", str(ledger_db)]).exit_code == 0
    db = sqlite3.connect(folder_db)
    db.execute("DELETE FROM memory")
    db.commit()
    db.close()
    out = runner.invoke(cli.app, ["check", "--db", str(ledger_db), "--settle", "0"]).output
    assert 'removed: 3f2a9c1e… "Greet me with Hi"' in out and UUID not in out


def test_status_says_entry_not_change(tmp_path):
    folder = tmp_path / "n"
    folder.mkdir()
    (folder / "a.md").write_text("x\n", encoding="utf-8")
    db = tmp_path / "l.db"
    runner.invoke(cli.app, ["add", str(folder), "--db", str(db)])
    out = runner.invoke(cli.app, ["status", "--db", str(db)]).output
    assert "last entry recorded" in out and "last change recorded" not in out


def test_whose_memories_to_read_is_remembered_so_an_empty_store_keeps_working(tmp_path):
    import json
    import sqlite3

    source = tmp_path / "w.db"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE memory (id VARCHAR, user_id VARCHAR, content TEXT, updated_at BIGINT, created_at BIGINT)")
    db.execute("INSERT INTO memory VALUES ('m1', 'the-user', 'likes tea', 1, 1)")
    db.commit()
    db.close()
    ledger_db = tmp_path / "l.db"
    assert runner.invoke(cli.app, ["add", str(source), "--db", str(ledger_db)]).exit_code == 0
    assert json.loads((tmp_path / "stores.json").read_text())["stores"][0]["user_id"] == "the-user"
    db = sqlite3.connect(source)
    db.execute("DELETE FROM memory")
    db.execute("INSERT INTO memory VALUES ('m9', 'a-second-user', 'someone else', 2, 2)")  # another user appears; only theirs exists now
    db.commit()
    db.close()
    out = runner.invoke(cli.app, ["check", "--db", str(ledger_db), "--settle", "0"])
    assert out.exit_code == 0 and "removed: m1" in out.output and "COULD NOT BE CHECKED" not in out.output and "someone else" not in out.output
    again = runner.invoke(cli.app, ["add", str(source), "--db", str(ledger_db)])  # the same database again is still recognised as already watched
    assert again.exit_code == 2 and "already watched" in again.output
