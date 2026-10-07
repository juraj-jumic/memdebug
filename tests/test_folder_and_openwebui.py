"""Stores that keep no history: a plain folder and Open WebUI's memory table. Both are read-only and record observed changes."""
import hashlib
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from memdebug.adapters.folder import FolderAdapter
from memdebug.adapters.openwebui import OpenWebUIAdapter
from memdebug.errors import AdapterError
from memdebug.ledger import Ledger
from memdebug.models import Op
from memdebug.sync import sync

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")


def digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def looks(adapter, ledger, scope):
    report = sync(adapter, ledger, scope, settle_seconds=0)
    return report, [(e.event.op, e.event.memory_id) for e in ledger.entries()]


# -- plain folder ----------------------------------------------------------------------------------------------------

@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    (root / "MEMORY.md").write_text("index\n", encoding="utf-8")
    (root / "topics").mkdir()
    (root / "topics" / "tea.md").write_text("likes tea\n", encoding="utf-8")
    (root / "notes.txt").write_text("not a memory\n", encoding="utf-8")
    return root


def test_the_folder_adapter_lists_only_markdown_and_never_writes(folder):
    before = digest(folder)
    adapter = FolderAdapter(folder)
    live = adapter.list_memories({"store": "memory"})
    assert sorted(m.id for m in live.memories) == ["MEMORY.md", "topics/tea.md"] and live.complete
    assert digest(folder) == before and adapter.capabilities == set() and adapter.read_history(10).events == []


def test_changes_in_a_folder_are_observed_not_flagged_as_outside_the_history(folder, tmp_path):
    adapter, ledger, scope = FolderAdapter(folder), Ledger(tmp_path / "l.db"), {"store": "memory"}
    report, ops = looks(adapter, ledger, scope)
    assert report.observed_events == 2 and report.external_events == 0 and {o for o, _ in ops} == {Op.ADD}
    (folder / "topics" / "tea.md").write_text("likes tea and cake\n", encoding="utf-8")
    (folder / "MEMORY.md").unlink()
    (folder / "new.md").write_text("fresh\n", encoding="utf-8")
    report, ops = looks(adapter, ledger, scope)
    assert report.observed_events == 3 and report.external_events == 0
    assert (Op.UPDATE, "topics/tea.md") in ops and (Op.DELETE, "MEMORY.md") in ops and (Op.ADD, "new.md") in ops
    assert Op.EXTERNAL not in {o for o, _ in ops} and looks(adapter, ledger, scope)[0].observed_events == 0  # and it settles


def test_a_git_repository_is_pointed_to_the_markdown_store_type(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(AdapterError, match="git repository"):
        FolderAdapter(repo)


@posix_only
def test_links_and_odd_names_are_skipped_and_the_listing_says_it_is_incomplete(folder, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    os.symlink(secret, folder / "link.md")
    os.symlink(tmp_path, folder / "linkdir")
    (folder / "CON.md").write_text("device name\n", encoding="utf-8")
    live = FolderAdapter(folder).list_memories({"store": "memory"})
    assert sorted(m.id for m in live.memories) == ["MEMORY.md", "topics/tea.md"] and not live.complete
    assert "TOP SECRET" not in "".join(m.text for m in live.memories)


def test_a_folder_that_is_a_file_or_missing_is_refused(tmp_path):
    (tmp_path / "f").write_text("x")
    with pytest.raises(AdapterError):
        FolderAdapter(tmp_path / "f")
    with pytest.raises(AdapterError):
        FolderAdapter(tmp_path / "nope")


# -- Open WebUI --------------------------------------------------------------------------------------------------------

def webui_db(path, rows=(("m1", "u1", "likes tea"),), users=("u1",), with_user_table=True):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE memory (id VARCHAR, user_id VARCHAR, content TEXT, updated_at BIGINT, created_at BIGINT, type VARCHAR, path TEXT, meta JSON)")
    for i, (mid, uid, text) in enumerate(rows):
        db.execute("INSERT INTO memory (id, user_id, content, updated_at, created_at) VALUES (?, ?, ?, ?, ?)", (mid, uid, text, 1000 + i, 1000 + i))
    if with_user_table:
        db.execute('CREATE TABLE "user" (id VARCHAR, email VARCHAR, password_hash TEXT)')
        for uid in users:
            db.execute('INSERT INTO "user" VALUES (?, ?, ?)', (uid, "a@example.com", "SECRET-HASH"))
    db.execute("CREATE TABLE auth (id VARCHAR, password TEXT)")
    db.execute("INSERT INTO auth VALUES ('u1', 'SECRET-PASSWORD')")
    db.commit()
    db.close()
    return path


def test_the_openwebui_adapter_reads_one_users_memories_read_only(tmp_path):
    path = webui_db(tmp_path / "webui.db", rows=[("m1", "u1", "likes tea"), ("m2", "u2", "someone else"), ("m3", "u1", "Croatian: čćšžđ")], users=("u1", "u2"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    adapter = OpenWebUIAdapter(path, user_id="u1")
    live = adapter.list_memories(adapter.scope)
    assert [(m.id, m.text) for m in live.memories] == [("m1", "likes tea"), ("m3", "Croatian: čćšžđ")] and live.complete
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["webui.db"]  # no -wal, -shm or journal created beside it


def test_it_works_out_the_single_user_and_asks_when_it_cannot(tmp_path):
    one = webui_db(tmp_path / "one.db")
    assert OpenWebUIAdapter(one).user == "u1"
    empty = webui_db(tmp_path / "empty.db", rows=[], users=("only",))
    assert OpenWebUIAdapter(empty).user == "only"  # no memories yet, but exactly one user
    many = webui_db(tmp_path / "many.db", rows=[("a", "u1", "x"), ("b", "u2", "y")], users=("u1", "u2"))
    with pytest.raises(AdapterError, match="--user-id"):
        OpenWebUIAdapter(many)
    nobody = webui_db(tmp_path / "nobody.db", rows=[], users=("a", "b"))
    with pytest.raises(AdapterError, match="--user-id"):
        OpenWebUIAdapter(nobody)


def test_other_tables_are_never_read(tmp_path, monkeypatch):
    path = webui_db(tmp_path / "w.db")
    seen = []
    real = sqlite3.connect

    def spying(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spying)
    adapter = OpenWebUIAdapter(path)
    adapter.list_memories(adapter.scope)
    text = " ".join(seen).lower()
    assert "auth" not in text and "password" not in text and "api_key" not in text and "oauth" not in text and "config" not in text


@pytest.mark.parametrize("bad", [None, 42, b"\xff\xfe invalid utf8", "x" * 300_000], ids=["null", "integer", "invalid-utf8", "huge"])
def test_hostile_rows_are_skipped_or_bounded_never_a_crash(tmp_path, bad):
    path = webui_db(tmp_path / "h.db")
    db = sqlite3.connect(path)
    db.execute("INSERT INTO memory (id, user_id, content, created_at, updated_at) VALUES ('bad', 'u1', ?, 5, 5)", (bad,))
    db.execute("INSERT INTO memory (id, user_id, content, created_at, updated_at) VALUES (NULL, 'u1', 'no id', 6, 6)")
    db.commit()
    db.close()
    adapter = OpenWebUIAdapter(path, user_id="u1")
    live = adapter.list_memories(adapter.scope)
    assert "likes tea" in [m.text for m in live.memories]
    assert all(len(m.text) <= 100_300 for m in live.memories)
    assert not live.complete or isinstance(bad, str)  # a row that had to be skipped marks the listing incomplete


def test_a_database_that_is_not_openwebuis_is_refused_clearly(tmp_path):
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE memory (x INTEGER)")
    db.commit()
    db.close()
    with pytest.raises(AdapterError, match="does not look like"):
        OpenWebUIAdapter(other)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(AdapterError):
        OpenWebUIAdapter(junk)
    with pytest.raises(AdapterError):
        OpenWebUIAdapter(tmp_path / "missing.db")


@posix_only
def test_a_database_path_you_name_may_be_a_link_to_a_file_but_not_to_anything_else(tmp_path):
    real = webui_db(tmp_path / "real.db")
    os.symlink(real, tmp_path / "link.db")
    assert OpenWebUIAdapter(tmp_path / "link.db").user == "u1"  # you chose this path yourself; the store's contents are what is untrusted
    (tmp_path / "adir").mkdir()
    os.symlink(tmp_path / "adir", tmp_path / "dirlink.db")
    with pytest.raises(AdapterError):
        OpenWebUIAdapter(tmp_path / "dirlink.db")


def test_the_scope_must_match_and_the_row_limit_marks_the_listing_incomplete(tmp_path):
    path = webui_db(tmp_path / "w.db", rows=[(f"m{i}", "u1", f"t{i}") for i in range(5)])
    adapter = OpenWebUIAdapter(path, max_rows=3)
    with pytest.raises(AdapterError, match="scope"):
        adapter.list_memories({"user_id": "someone-else"})
    live = adapter.list_memories(adapter.scope)
    assert len(live.memories) == 3 and not live.complete


def test_openwebui_changes_are_observed_over_time(tmp_path):
    path = webui_db(tmp_path / "w.db")
    adapter, ledger = OpenWebUIAdapter(path), Ledger(tmp_path / "l.db")
    looks(adapter, ledger, adapter.scope)
    db = sqlite3.connect(path)
    db.execute("UPDATE memory SET content = 'likes coffee' WHERE id = 'm1'")
    db.execute("INSERT INTO memory (id, user_id, content, created_at, updated_at) VALUES ('m2', 'u1', 'new one', 9, 9)")
    db.commit()
    db.close()
    report, ops = looks(adapter, ledger, adapter.scope)
    assert report.observed_events == 2 and (Op.UPDATE, "m1") in ops and (Op.ADD, "m2") in ops and Op.EXTERNAL not in {o for o, _ in ops}
    info = ledger.save_snapshot(adapter.name, adapter.scope, adapter.list_memories(adapter.scope).memories, complete=True, taken_at=datetime.now(timezone.utc))
    assert info.count == 2
