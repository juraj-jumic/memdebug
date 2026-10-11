"""What a watcher that runs without a terminal needs: a small private log, a store list that is read again, and a problem said once."""
import os
import stat

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.monitor as mon
import memdebug.stores as st
from memdebug.errors import MemdebugError
from memdebug.ledger import Ledger
from memdebug.logfile import LogFile
from test_stores_and_monitor import notes_folder

runner = CliRunner()
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")


# -- the log file ---------------------------------------------------------------------------------------------------------------

def test_lines_are_appended_one_per_line_and_the_folder_is_made(tmp_path):
    log = LogFile(tmp_path / "deep" / "watch.log")
    log("first")
    log("second")
    assert (tmp_path / "deep" / "watch.log").read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_every_line_is_cleaned_and_shortened_like_everything_else_memdebug_shows(tmp_path):
    log = LogFile(tmp_path / "w.log")
    log("a\x1b[2J b\r\nfake second line‮" + "x" * 5000)
    text = (tmp_path / "w.log").read_text(encoding="utf-8")
    assert text.count("\n") == 1 and "\x1b" not in text and "\r" not in text and "‮" not in text and len(text) < 900


def test_the_file_never_grows_without_bound(tmp_path):
    log = LogFile(tmp_path / "w.log", max_bytes=200)
    for i in range(40):
        log(f"line {i:03d} " + "x" * 30)
    assert (tmp_path / "w.log").stat().st_size <= 200 + 60 and (tmp_path / "w.log.1").exists()
    older = (tmp_path / "w.log.1").read_text(encoding="utf-8")
    assert "line 039" not in older and "line 039" in (tmp_path / "w.log").read_text(encoding="utf-8")  # the newest lines are in the live file


def test_a_log_that_cannot_be_written_never_stops_the_watching(tmp_path):
    (tmp_path / "blocker").write_text("a file where a folder is needed", encoding="utf-8")
    LogFile(tmp_path / "blocker" / "w.log")("line")  # no error


def test_it_never_writes_into_a_folder_or_through_a_link(tmp_path):
    folder = tmp_path / "w.log"
    folder.mkdir()
    LogFile(folder)("line")
    assert list(folder.iterdir()) == []
    if os.name == "posix":
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        os.symlink(victim, tmp_path / "linked.log")
        LogFile(tmp_path / "linked.log")("line")
        assert victim.read_text(encoding="utf-8") == "keep"


@posix_only
def test_the_log_is_private_to_the_user(tmp_path):
    LogFile(tmp_path / "w.log")("line")
    assert stat.S_IMODE((tmp_path / "w.log").stat().st_mode) == 0o600


# -- watch -----------------------------------------------------------------------------------------------------------------------

def make_watch(tmp_path):
    folder = notes_folder(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    cfg = st.StoreConfig("notes", "folder", str(folder))
    mon.baseline(cfg, ledger)
    return folder, ledger, cfg


def run_watch(registry, ledger, cycles, **kw):
    said = []
    mon.watch(registry, ledger, every=5, say=said.append, ring=lambda: None, cycles=cycles, sleep=kw.pop("sleep", lambda s: None), settle=0, **kw)
    return said


def test_a_store_added_while_watching_is_picked_up_and_announced(tmp_path):
    folder, ledger, cfg = make_watch(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("one\n", encoding="utf-8")
    second = st.StoreConfig("other", "folder", str(other))
    mon.baseline(second, ledger)
    registries = iter([st.Registry([cfg]), st.Registry([cfg, second])])
    current = {"value": st.Registry([cfg])}

    def sleep(seconds):
        current["value"] = next(registries, current["value"])
        (other / "x.md").write_text("two\n", encoding="utf-8")  # a change in the store that was just added

    said = run_watch(st.Registry([cfg]), ledger, cycles=3, sleep=sleep, reload=lambda: current["value"])
    text = "\n".join(said)
    assert "The list of watched stores changed: now 2 store(s)." in text and "other" in text and "edited" in text


def test_a_settings_file_that_cannot_be_read_at_that_moment_keeps_the_previous_list(tmp_path):
    folder, ledger, cfg = make_watch(tmp_path)

    def broken():
        raise MemdebugError("the settings file is damaged")

    def sleep(seconds):
        (folder / "a.md").write_text("changed while the settings were unreadable\n", encoding="utf-8")

    said = run_watch(st.Registry([cfg]), ledger, cycles=2, sleep=sleep, reload=broken)
    text = "\n".join(said)
    assert "edited" in text and "list of watched stores changed" not in text  # still watching the same store, and no false announcement


def test_without_reload_the_list_never_changes(tmp_path):
    folder, ledger, cfg = make_watch(tmp_path)
    assert "changed: now" not in "\n".join(run_watch(st.Registry([cfg]), ledger, cycles=2))


def test_a_failing_ledger_is_said_once_not_at_every_look_and_again_after_it_recovers(tmp_path, monkeypatch):
    folder, ledger, cfg = make_watch(tmp_path)
    states = iter([True, False, False, False, True, False])
    real = mon.check_all

    def check(*a, **k):
        summary = real(*a, **k)
        summary.ledger_ok = next(states, True)
        return summary

    monkeypatch.setattr(mon, "check_all", check)
    said = run_watch(st.Registry([cfg]), ledger, cycles=6)
    assert sum("PROBLEM: the ledger failed" in line for line in said) == 2  # once when it began, once more when it broke again


def test_the_watch_command_writes_what_it_says_to_the_log_file(tmp_path):
    folder = notes_folder(tmp_path)
    db, log = tmp_path / "l.db", tmp_path / "logs" / "watch.log"
    assert runner.invoke(cli.app, ["add", str(folder), "--name", "n", "--db", str(db)]).exit_code == 0
    (folder / "a.md").write_text("an edit made after memdebug started watching\n", encoding="utf-8")
    out = runner.invoke(cli.app, ["watch", "--once", "--no-bell", "--settle", "0", "--log", str(log), "--db", str(db)])
    assert out.exit_code == 0 and "Watching 1 store(s)" in out.output
    logged = log.read_text(encoding="utf-8")
    assert "Watching 1 store(s)" in logged and "edited" in logged


def test_without_log_nothing_is_written_anywhere(tmp_path):
    folder = notes_folder(tmp_path)
    db = tmp_path / "l.db"
    assert runner.invoke(cli.app, ["add", str(folder), "--name", "n", "--db", str(db)]).exit_code == 0
    assert runner.invoke(cli.app, ["watch", "--once", "--no-bell", "--settle", "0", "--db", str(db)]).exit_code == 0
    assert not list(tmp_path.glob("**/*.log"))


def test_the_watch_command_gives_the_watcher_a_way_to_read_the_store_list_again(tmp_path, monkeypatch):
    folder = notes_folder(tmp_path)
    db = tmp_path / "l.db"
    assert runner.invoke(cli.app, ["add", str(folder), "--name", "first", "--db", str(db)]).exit_code == 0
    seen = {}
    monkeypatch.setattr(cli, "watch", lambda registry, ledger, **kw: seen.update(kw, registry=registry))
    assert runner.invoke(cli.app, ["watch", "--once", "--db", str(db)]).exit_code == 0
    reload = seen["reload"]
    assert [s.name for s in reload().stores] == ["first"]
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("x\n", encoding="utf-8")
    assert runner.invoke(cli.app, ["add", str(other), "--name", "second", "--db", str(db)]).exit_code == 0
    assert [s.name for s in reload().stores] == ["first", "second"]  # read from the settings file each time it is called
