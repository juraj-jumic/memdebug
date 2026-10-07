"""The registry of stores, finding likely stores, and check/status/watch/setup."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.monitor as mon
import memdebug.stores as st
from memdebug.ledger import Ledger
from memdebug.models import Op
from payloads import PAYLOAD_IDS, PAYLOADS

runner = CliRunner()
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def notes_folder(tmp_path, name="notes"):
    root = tmp_path / name
    root.mkdir()
    (root / "a.md").write_text("likes tea\n", encoding="utf-8")
    return root


def git_repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    run = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=a", "-c", "user.email=a@x", "-c", "commit.gpgsign=false", *a], check=True, env=env, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    (root / "p.md").write_text("good\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "first")
    return root


def webui_copy(tmp_path):
    path = tmp_path / "webui.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE memory (id VARCHAR, user_id VARCHAR, content TEXT, updated_at BIGINT, created_at BIGINT)")
    db.execute("INSERT INTO memory VALUES ('m1', 'u1', 'likes tea', 1, 1)")
    db.commit()
    db.close()
    return path


# -- the settings file -------------------------------------------------------------------------------------------------

def test_the_settings_survive_a_save_and_load_and_are_private(tmp_path):
    reg = st.Registry()
    reg.add(st.StoreConfig("notes", "folder", "/x/notes"))
    reg.add(st.StoreConfig("mem", "mem0", "/x/history.db", user_id="u"))
    reg.witness = "/w/witness.txt"
    path = tmp_path / "data" / "stores.json"
    st.save_registry(reg, path)
    back = st.load_registry(path)
    assert [s.name for s in back.stores] == ["notes", "mem"] and back.witness == "/w/witness.txt" and back.get("mem").user_id == "u"
    assert not [p for p in path.parent.iterdir() if p.name != "stores.json"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    assert st.load_registry(tmp_path / "missing.json").stores == []


@pytest.mark.parametrize("document", [
    "not json", "[]", "{}", '{"version": 2, "stores": []}', '{"version": 1, "stores": "x"}', '{"version": 1, "extra": 1}',
    '{"version": 1, "stores": [{"name": "A", "kind": "folder", "path": "/x"}]}', '{"version": 1, "stores": [{"name": "../x", "kind": "folder", "path": "/x"}]}',
    '{"version": 1, "stores": [{"name": "a", "kind": "evil", "path": "/x"}]}', '{"version": 1, "stores": [{"name": "a", "kind": "folder"}]}',
    '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": ""}]}', '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": "/x", "run": 1}]}',
    '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": 5}]}', '{"version": 1, "stores": [{"name": "a", "kind": "mem0", "path": "/x"}]}',
    '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": "/x"}, {"name": "a", "kind": "folder", "path": "/y"}]}',
    '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": "/x\\u0000y"}]}', '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": "/x\\u001b[2J"}]}',
    '{"version": 1, "stores": [{"name": "a", "kind": "folder", "path": "/x", "user_id": ["a"]}]}', '{"version": 1, "witness": 7}',
], ids=lambda d: d[:28])
def test_a_damaged_or_hostile_settings_file_is_refused_with_a_clear_message(tmp_path, document):
    path = tmp_path / "stores.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(st.SettingsError):
        st.load_registry(path)


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_whatever_is_in_the_settings_file_it_either_loads_cleanly_or_is_refused(tmp_path, payload):
    for field in ("name", "path", "user_id"):
        entry = {"name": "ok", "kind": "mem0", "path": "/x", "user_id": "u"}
        entry[field] = payload
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"version": 1, "stores": [entry]}), encoding="utf-8")
        try:
            reg = st.load_registry(path)
        except st.SettingsError:
            continue
        assert len(reg.stores) == 1 and not any(ch in reg.stores[0].path for ch in "\0\x1b")


def test_an_oversized_settings_file_and_a_link_are_refused(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(" " * (st.MAX_SETTINGS_BYTES + 1), encoding="utf-8")
    with pytest.raises(st.SettingsError, match="plain file"):
        st.load_registry(big)
    if os.name == "posix":
        real = tmp_path / "real.json"
        real.write_text('{"version": 1, "stores": []}', encoding="utf-8")
        os.symlink(real, tmp_path / "link.json")
        with pytest.raises(st.SettingsError, match="plain file"):
            st.load_registry(tmp_path / "link.json")


def test_names_are_validated(tmp_path):
    assert all(st.valid_name(n) for n in ("a", "notes", "my-notes.2", "x_1")) and not any(st.valid_name(n) for n in ("", "A", "-x", "a b", "a/b", "..", "x" * 41))
    reg = st.Registry()
    reg.add(st.StoreConfig("a", "folder", "/x"))
    with pytest.raises(st.SettingsError, match="already"):
        reg.add(st.StoreConfig("a", "folder", "/y"))
    with pytest.raises(st.SettingsError, match="no store"):
        reg.remove("zzz")


# -- telling what a path is ----------------------------------------------------------------------------------------------

@needs_git
def test_the_kind_of_a_path_is_worked_out_from_its_structure(tmp_path):
    assert st.detect_kind(notes_folder(tmp_path)) == "folder" and st.detect_kind(git_repo(tmp_path)) == "markdown"
    assert st.detect_kind(webui_copy(tmp_path)) == "openwebui"
    mem = tmp_path / "history.db"
    db = sqlite3.connect(mem)
    db.execute("CREATE TABLE history (id TEXT)")
    db.commit()
    db.close()
    assert st.detect_kind(mem) == "mem0"
    junk = tmp_path / "junk.txt"
    junk.write_text("hello", encoding="utf-8")
    for bad in (junk, tmp_path / "nope"):
        with pytest.raises(st.SettingsError):
            st.detect_kind(bad)
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE things (x)")
    db.commit()
    db.close()
    with pytest.raises(st.SettingsError, match="not a database"):
        st.detect_kind(other)


# -- finding likely stores --------------------------------------------------------------------------------------------------

def fake_home(tmp_path):
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    for name in ("-home-alex-shop", "-home-alex-blog", "C--Users-Alex-Projects-Thing"):
        (projects / name / "memory").mkdir(parents=True)
        (projects / name / "memory" / "MEMORY.md").write_text("index\n", encoding="utf-8")
    (projects / "-no-markdown" / "memory").mkdir(parents=True)
    (projects / "-no-markdown" / "memory" / "x.txt").write_text("x", encoding="utf-8")
    (projects / "-no-memory-folder").mkdir()
    return home


def test_likely_stores_are_found_with_valid_unique_names_and_without_reading_files(tmp_path):
    home = fake_home(tmp_path)
    if os.name == "posix":
        for f in home.rglob("MEMORY.md"):
            f.chmod(0)  # unreadable: finding them must not need to read them
    try:
        found = st.discover(home)
    finally:
        for f in home.rglob("MEMORY.md"):
            f.chmod(0o644)
    assert len(found) == 3 and all(c.kind == "folder" and st.valid_name(c.name) for c in found)
    assert len({c.name for c in found}) == 3 and {c.path.parent.name for c in found} == {"-home-alex-shop", "-home-alex-blog", "C--Users-Alex-Projects-Thing"}


@posix_only
def test_links_and_odd_folder_names_are_not_offered(tmp_path):
    home = fake_home(tmp_path)
    projects = home / ".claude" / "projects"
    outside = tmp_path / "outside"
    (outside / "memory").mkdir(parents=True)
    (outside / "memory" / "x.md").write_text("x", encoding="utf-8")
    os.symlink(outside, projects / "-linked-project")  # a project folder that is a link
    (projects / "-link-memory").mkdir()
    os.symlink(outside / "memory", projects / "-link-memory" / "memory")  # a memory folder that is a link
    (projects / "-ctrl\x1bchars" / "memory").mkdir(parents=True)
    (projects / "-ctrl\x1bchars" / "memory" / "x.md").write_text("x", encoding="utf-8")
    assert {c.path.parent.name for c in st.discover(home)} == {"-home-alex-shop", "-home-alex-blog", "C--Users-Alex-Projects-Thing"}


def test_a_computer_with_nothing_to_find_or_too_much_is_handled(tmp_path):
    assert st.discover(tmp_path / "empty-home") == []
    home = tmp_path / "busy"
    for i in range(80):
        (home / ".claude" / "projects" / f"-p{i:03}" / "memory").mkdir(parents=True)
        (home / ".claude" / "projects" / f"-p{i:03}" / "memory" / "m.md").write_text("x", encoding="utf-8")
    assert len(st.discover(home)) == 50


# -- checking ---------------------------------------------------------------------------------------------------------------

def registry_for(tmp_path, *stores):
    reg = st.Registry()
    for s in stores:
        reg.add(s)
    return reg


def test_check_reports_what_changed_then_goes_quiet_and_never_alarms_for_a_folder(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    reg = registry_for(tmp_path, cfg)
    assert mon.check_all(reg, ledger, settle=0).results[0].quiet
    (folder / "a.md").write_text("likes tea and an instruction\n", encoding="utf-8")
    (folder / "b.md").write_text("new\n", encoding="utf-8")
    summary = mon.check_all(reg, ledger, settle=0)
    result = summary.results[0]
    assert sorted(result.changes, key=lambda c: c[1]) == [(Op.UPDATE, "a.md"), (Op.ADD, "b.md")] and not summary.attention and summary.exit_code == 0
    assert "2 changes noticed" in mon.describe(result) and mon.check_all(reg, ledger, settle=0).results[0].quiet


@needs_git
def test_an_edit_that_bypassed_git_is_attention_and_exit_code_1(tmp_path):
    repo = git_repo(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("repo", "markdown", str(repo))
    mon.baseline(cfg, ledger)
    (repo / "p.md").write_text("good, then planted\n", encoding="utf-8")
    summary = mon.check_all(registry_for(tmp_path, cfg), ledger, settle=0)
    assert summary.attention and summary.exit_code == 1 and "ATTENTION" in mon.describe(summary.results[0])
    assert summary.results[0].outside_history == 1


def test_one_store_failing_never_stops_the_others(tmp_path, monkeypatch):
    good = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    reg = registry_for(tmp_path, st.StoreConfig("gone", "folder", str(tmp_path / "vanished")), st.StoreConfig("good", "folder", str(good)),
                       st.StoreConfig("good2", "folder", str(notes_folder(tmp_path, "n2"))))
    real = st.open_store

    def flaky(cfg):
        if cfg.name == "good2":
            raise ValueError("surprise")
        return real(cfg)

    monkeypatch.setattr(mon, "open_store", flaky)
    summary = mon.check_all(reg, ledger, settle=0)
    by = {r.store.name: r for r in summary.results}
    assert "cannot access" in by["gone"].error and by["good2"].error == "unexpected error (ValueError)" and by["good"].error is None and by["good"].changes
    assert summary.failed and not summary.attention and summary.exit_code == 2


def test_a_broken_ledger_is_attention(tmp_path):
    folder = notes_folder(tmp_path)
    path = tmp_path / "l.db"
    ledger = Ledger(path)
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    ledger.close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE events SET payload = replace(payload, 'likes tea', 'evil')")
    raw.commit()
    raw.close()
    summary = mon.check_all(registry_for(tmp_path, cfg), Ledger(path), settle=0)
    assert summary.attention and not summary.ledger_ok and "PROBLEM" in "\n".join(mon.summary_lines(summary))


def test_the_witness_is_updated_after_a_check_and_a_damaged_one_is_reported_not_extended(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    reg = registry_for(tmp_path, cfg)
    reg.witness = str(tmp_path / "w.txt")
    summary = mon.check_all(reg, ledger, settle=0, ledger_path=tmp_path / "l.db")
    assert "witness updated" in summary.witness_note and "same disk" in summary.witness_warning
    assert "up to date" in mon.check_all(reg, ledger, settle=0).witness_note
    Path(reg.witness).write_text("garbage\n", encoding="utf-8")
    (folder / "x.md").write_text("new\n", encoding="utf-8")
    note = mon.check_all(reg, ledger, settle=0).witness_note
    assert "could not be updated" in note and Path(reg.witness).read_text() == "garbage\n"


def test_status_describes_without_changing_anything(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    reg = registry_for(tmp_path, cfg, st.StoreConfig("gone", "folder", str(tmp_path / "vanished")))
    (folder / "a.md").write_text("changed but not yet checked\n", encoding="utf-8")
    before = (ledger.counts()["events"], digest(folder))
    lines = mon.status_lines(reg, ledger, now=datetime.now(timezone.utc) + timedelta(hours=3))
    assert "1 memories" in lines[0] and "last snapshot s1, 3 hours ago" in lines[0] and "cannot be read" in lines[1]
    assert (ledger.counts()["events"], digest(folder)) == before


def test_ages_read_naturally():
    now = T0
    assert [mon.ago(now - timedelta(seconds=s), now) for s in (1, 59, 600, 7200, 3 * 86400)] == ["1 second ago", "59 seconds ago", "10 minutes ago", "2 hours ago", "3 days ago"]


def test_watch_prints_only_what_is_worth_knowing(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    reg = registry_for(tmp_path, cfg, st.StoreConfig("gone", "folder", str(tmp_path / "vanished")))
    said, rang, slept = [], [], []
    steps = iter([lambda: (folder / "n.md").write_text("new\n", encoding="utf-8"), lambda: None])

    def fake_sleep(seconds):
        slept.append(seconds)
        next(steps)()

    mon.watch(reg, ledger, every=1, say=said.append, ring=lambda: rang.append(1), cycles=3, sleep=fake_sleep, settle=0, clock=lambda: T0)
    assert said[0].startswith("Watching 2 store(s) every 5 seconds")  # never faster than 5 seconds
    text = "\n".join(said)
    assert text.count("COULD NOT BE CHECKED") == 1  # the same error is not repeated every cycle
    assert text.count("added: n.md") == 1 and not rang and slept == [5, 5]


# -- the commands ------------------------------------------------------------------------------------------------------------

def run(db, *args, **kw):
    return runner.invoke(cli.app, [*args, "--db", str(db)], **kw)


def test_add_registers_takes_a_baseline_and_guards_against_mistakes(tmp_path):
    db, folder = tmp_path / "d" / "l.db", notes_folder(tmp_path)
    (db.parent).mkdir()
    ok = run(db, "add", str(folder))
    assert ok.exit_code == 0 and "Added notes" in ok.output and "first snapshot, s1" in ok.output
    assert [s.name for s in st.load_registry(db.with_name("stores.json")).stores] == ["notes"]
    other = notes_folder(tmp_path, "other")
    again = run(db, "add", str(folder))  # the same folder twice would record every change twice
    assert again.exit_code == 2 and "already watched as notes" in again.output
    dup = run(db, "add", str(other), "--name", "notes")
    assert dup.exit_code == 2 and "already a store called notes" in dup.output
    assert run(db, "add", str(tmp_path / "nowhere")).exit_code == 2
    assert run(db, "add", str(other), "--type", "evil").exit_code == 2
    assert run(db, "add", str(other), "--name", "Bad Name").exit_code == 2
    assert [s.name for s in st.load_registry(db.with_name("stores.json")).stores] == ["notes"]  # failed adds changed nothing


def test_stores_remove_and_the_friendly_empty_states(tmp_path):
    db = tmp_path / "l.db"
    assert "memdebug setup" in run(db, "stores").output and run(db, "check").exit_code == 2 and "memdebug setup" in run(db, "check").output
    assert "memdebug setup" in run(db, "status").output and run(db, "watch", "--once").exit_code == 2
    folder = notes_folder(tmp_path)
    run(db, "add", str(folder), "--no-baseline")
    assert "notes: a folder of markdown notes" in run(db, "stores").output
    removed = run(db, "remove", "notes")
    assert removed.exit_code == 0 and "stays in the ledger" in removed.output and run(db, "remove", "notes").exit_code == 2


@needs_git
def test_check_exit_codes_tell_you_when_something_needs_a_look(tmp_path):
    db, repo = tmp_path / "l.db", git_repo(tmp_path)
    run(db, "add", str(repo))
    assert run(db, "check", "--settle", "0").exit_code == 0
    (repo / "p.md").write_text("planted\n", encoding="utf-8")
    bad = run(db, "check", "--settle", "0")
    assert bad.exit_code == 1 and "ATTENTION" in bad.output and "memdebug serve" in bad.output


def test_watch_once_and_status(tmp_path):
    db, folder = tmp_path / "l.db", notes_folder(tmp_path)
    run(db, "add", str(folder))
    (folder / "z.md").write_text("new\n", encoding="utf-8")
    watched = run(db, "watch", "--once", "--settle", "0")
    assert watched.exit_code == 0 and "Watching 1 store(s)" in watched.output and "added: z.md" in watched.output
    status = run(db, "status")
    assert "1 store(s) watched; ledger intact." in status.output and "notes (folder): 2 memories" in status.output


def with_home(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_setup_yes_finds_registers_snapshots_and_changes_nothing_in_the_memory(tmp_path, monkeypatch):
    home = fake_home(tmp_path)
    with_home(monkeypatch, home)
    before = digest(home)
    db = tmp_path / "data" / "l.db"
    db.parent.mkdir()
    result = run(db, "setup", "--yes")
    assert result.exit_code == 0 and result.output.count("Done: s") == 3 and "memdebug check" in result.output
    assert len(st.load_registry(db.with_name("stores.json")).stores) == 3 and digest(home) == before
    again = run(db, "setup", "--yes")
    assert "Nothing was found automatically" in again.output and len(st.load_registry(db.with_name("stores.json")).stores) == 3  # nothing added twice


def test_setup_asks_adds_by_path_and_offers_a_witness(tmp_path, monkeypatch):
    with_home(monkeypatch, tmp_path / "empty-home")
    folder, witness_dir = notes_folder(tmp_path), tmp_path / "usb"
    witness_dir.mkdir()
    db = tmp_path / "data" / "l.db"
    db.parent.mkdir()
    answers = f"{tmp_path / 'nowhere'}\n{folder}\n\n{witness_dir}\n"
    result = run(db, "setup", input=answers)
    assert result.exit_code == 0 and "Could not use that" in result.output and "Will watch it as a folder of markdown notes" in result.output
    registry = st.load_registry(db.with_name("stores.json"))
    assert [s.name for s in registry.stores] == ["notes"] and registry.witness == str((witness_dir / "memdebug-witness.txt").resolve())
    assert (witness_dir / "memdebug-witness.txt").is_file() and "same disk" in result.output


def test_setup_on_a_bare_computer_ends_helpfully(tmp_path, monkeypatch):
    with_home(monkeypatch, tmp_path / "empty-home")
    result = run(tmp_path / "l.db", "setup", "--yes")
    assert result.exit_code == 0 and "Nothing was found automatically" in result.output and "memdebug add <path>" in result.output


def test_a_windows_junction_in_place_of_a_memory_folder_is_not_offered(tmp_path, monkeypatch):
    home = fake_home(tmp_path)
    monkeypatch.setattr(st, "_is_reparse_point", lambda info: True)
    assert st.discover(home) == []
