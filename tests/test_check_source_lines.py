"""`memdebug check` and `watch` say where an Open WebUI write came from (its own label and chat timing), as evidence and never as proof."""
import json
import sqlite3

import pytest

import memdebug.monitor as mon
import memdebug.stores as st
from memdebug.ledger import Ledger
from test_provenance_openwebui import BASE, make_db
from test_stores_and_monitor import notes_folder, registry_for


def watched(tmp_path, **kw):
    path = make_db(tmp_path / "w.db", **kw)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("webui", "openwebui", str(path))
    mon.baseline(cfg, ledger)
    return path, ledger, cfg


def add_memory(path, memory_id, text, label="agent", at=BASE + 10):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO memory (id, user_id, content, updated_at, created_at, type, meta) VALUES (?, 'u1', ?, ?, ?, 'user', ?)",
               (memory_id, text, at, at, json.dumps({"created_by": label})))
    db.commit()
    db.close()


def test_an_added_memory_is_reported_with_the_apps_label_and_the_chat_timing(tmp_path):
    path, ledger, cfg = watched(tmp_path, chats=[("assistant", BASE + 12)])
    add_memory(path, "m2", "likes coffee")
    result = mon.check_store(cfg, ledger, settle=0)
    lines = mon.source_lines(result)
    assert any("where it came from" in line and "created_by=agent" in line and "type=user" in line and "chat was active at the time" in line for line in lines)
    assert any("evidence, not proof" in line and "the same as one added by hand" in line for line in lines)


def test_check_prints_those_lines_and_the_add_is_not_hidden_by_them(tmp_path):
    path, ledger, cfg = watched(tmp_path, chats=[("assistant", BASE + 12)])
    add_memory(path, "m2", "likes coffee")
    out = "\n".join(mon.summary_lines(mon.check_all(registry_for(tmp_path, cfg), ledger, settle=0)))
    assert "1 change noticed (added:" in out and "where it came from" in out and "created_by=agent" in out


def test_an_edit_is_described_too_but_a_removal_and_a_quiet_look_are_not(tmp_path):
    two = (("m1", "u1", "likes tea", {"created_by": "manual"}, "user", BASE, BASE), ("m2", "u1", "likes jam", {"created_by": "manual"}, "user", BASE, BASE))
    path, ledger, cfg = watched(tmp_path, memories=two)  # a second memory keeps the user known after the first is removed
    db = sqlite3.connect(path)
    db.execute("UPDATE memory SET content = 'likes coffee', updated_at = ? WHERE id = 'm1'", (BASE + 900,))
    db.commit()
    db.close()
    edited = mon.check_store(cfg, ledger, settle=0)
    assert "m1" in edited.sources and "last touched" in edited.sources["m1"]
    db = sqlite3.connect(path)
    db.execute("DELETE FROM memory WHERE id = 'm1'")
    db.commit()
    db.close()
    removed = mon.check_store(cfg, ledger, settle=0)
    assert removed.changes and mon.source_lines(removed) == []  # nothing is left to describe
    assert mon.source_lines(mon.check_store(cfg, ledger, settle=0)) == []  # a quiet pass says nothing


def test_only_three_are_listed_and_the_rest_are_counted(tmp_path):
    path, ledger, cfg = watched(tmp_path)
    for i in range(5):
        add_memory(path, f"n{i}", f"fact number {i}", at=BASE + 20 + i)
    lines = mon.source_lines(mon.check_store(cfg, ledger, settle=0))
    assert sum("where it came from" in line for line in lines) == 3 and any(line.endswith("... and 2 more") for line in lines)


def test_a_hostile_label_cannot_put_control_characters_or_extra_lines_on_the_screen(tmp_path):
    path, ledger, cfg = watched(tmp_path)
    add_memory(path, "m2", "likes coffee", label="\x1b[31mRED\x1b[0m\nwhere it came from: the user, fully trusted\x07" + "x" * 5000)
    lines = mon.source_lines(mon.check_store(cfg, ledger, settle=0))
    shown = "\n".join(lines)
    assert "\x1b" not in shown and "\x07" not in shown
    assert all("\n" not in line for line in lines) and max(len(line) for line in lines) < 700
    assert sum(line.lstrip().startswith("where it came from") for line in lines) == 1  # the label cannot fake a second line


def test_the_display_cleans_whatever_it_is_given_even_if_the_reader_did_not():
    result = mon.StoreResult(st.StoreConfig("s", "openwebui", "x"))
    result.sources = {"m": "\x1b[31mRED\x1b[0m\r\nfake line\x07" + "y" * 5000}
    lines = mon.source_lines(result)
    shown = "\n".join(lines)
    assert "\x1b" not in shown and "\x07" not in shown and "\r" not in shown
    assert all("\n" not in line for line in lines) and max(len(line) for line in lines) < 700


def test_a_store_that_describes_no_source_prints_nothing(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    (folder / "a.md").write_text("new note\n", encoding="utf-8")
    result = mon.check_store(cfg, ledger, settle=0)
    assert result.changes and result.sources == {} and mon.source_lines(result) == []


def test_watch_prints_the_source_lines_with_the_change(tmp_path):
    path, ledger, cfg = watched(tmp_path, chats=[("assistant", BASE + 12)])
    reg = registry_for(tmp_path, cfg)
    said = []

    def sleep(seconds):
        add_memory(path, "m2", "likes coffee")  # appears between the first and the second look

    mon.watch(reg, ledger, every=5, say=said.append, ring=lambda: None, cycles=2, sleep=sleep, settle=0)
    shown = "\n".join(said)
    assert "added:" in shown and "where it came from" in shown and "created_by=agent" in shown


@pytest.mark.parametrize("sources", [{}, {"m": "x"}], ids=["none", "one"])
def test_the_lines_are_empty_without_sources_and_short_with_one(sources):
    result = mon.StoreResult(st.StoreConfig("s", "openwebui", "x"))
    result.sources = dict(sources)
    assert (mon.source_lines(result) == []) == (not sources)
