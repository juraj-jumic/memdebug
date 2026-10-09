"""Windows paths longer than 259 characters.

Windows refuses them unless long paths are switched on in the registry, which is off by default. The integration tests below run on Windows only and
use the `default_windows_path_limit` fixture, which makes Python's file functions refuse such paths the way a default Windows does, even on a machine
that has long paths on. Before long paths were handled, a note beyond the limit was silently left out of the baseline and of every check, the rollback
said "nothing to restore" while the note stayed tampered, and a backup beyond the limit made the rollback fail. The path rules themselves are tested
on every platform.
"""
import builtins
import io
import json
import ntpath
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.stores as st
from memdebug import longpath
from memdebug.agents import scan_agents
from memdebug.provenance import find_writers

windows_only = pytest.mark.skipif(os.name != "nt", reason="the path limit and the extended-length form are Windows things")
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
runner = CliRunner()


# -- the rules, on every platform -----------------------------------------------------------------------------------------------

FS_CASES = {
    "drive": (r"C:\notes\a.md", r"\\?\C:\notes\a.md"),
    "slashes": ("C:/notes/a.md", r"\\?\C:\notes\a.md"),
    "dots": (r"C:\notes\.\x\..\a.md", r"\\?\C:\notes\a.md"),
    "trailing": ("C:\\notes\\", r"\\?\C:\notes"),
    "root": ("C:\\", "\\\\?\\C:\\"),
    "unc": (r"\\server\share\notes\a.md", r"\\?\UNC\server\share\notes\a.md"),
    "git-name": ("C:/repo/.git/MERGE_HEAD", r"\\?\C:\repo\.git\MERGE_HEAD"),  # git prints forward slashes; they must not reach the prefix form
    "prefixed": (r"\\?\C:\notes\a.md", r"\\?\C:\notes\a.md"),
    "device": ("\\\\.\\pipe\\x", "\\\\.\\pipe\\x"),
}


@pytest.mark.parametrize("given, expected", list(FS_CASES.values()), ids=list(FS_CASES))
def test_a_windows_path_is_given_to_the_system_in_extended_length_form(given, expected):
    assert longpath.fs(given, _windows=True) == expected


def test_other_platforms_get_their_path_back_unchanged():
    for given in ("/home/me/notes/a.md", "relative/a.md", r"C:\notes\a.md"):
        assert longpath.fs(given, _windows=False) == given


PLAIN_CASES = {
    "drive": (r"\\?\C:\notes\a.md", r"C:\notes\a.md"),
    "unc": (r"\\?\UNC\server\share\a.md", r"\\server\share\a.md"),
    "already": (r"C:\notes\a.md", r"C:\notes\a.md"),
    "not-a-drive": (r"\\?\Volume{1234}\a.md", r"\\?\Volume{1234}\a.md"),  # not something fs() makes, so left alone
    "posix": ("/home/me/a.md", "/home/me/a.md"),
}


@pytest.mark.parametrize("given, expected", list(PLAIN_CASES.values()), ids=list(PLAIN_CASES))
def test_the_ordinary_form_is_got_back_by_taking_the_prefix_off(given, expected):
    assert longpath.plain(given) == expected


@pytest.mark.parametrize("path", [r"C:\a\b" + "\\x" * 150 + r"\n.md", r"\\srv\share\a" + "\\y" * 150 + ".md"], ids=["drive", "unc"])
def test_converting_and_converting_back_gives_the_same_path(path):
    assert longpath.plain(longpath.fs(path, _windows=True)) == ntpath.normpath(path)


# -- the limit, simulated -------------------------------------------------------------------------------------------------------

@pytest.fixture
def default_windows_path_limit(monkeypatch):
    """Python's file functions refuse a path longer than 259 characters (247 for a folder), as a Windows without long paths does. Paths in the
    extended-length form are exempt, as on real Windows."""
    if os.name != "nt":
        pytest.skip("the limit is a Windows thing")
    limit, folder_limit = 259, 247
    absolute = os.path.abspath

    def too_long(path, bound=limit):
        try:
            text = os.fspath(path)
        except TypeError:
            return False
        text = os.fsdecode(text) if isinstance(text, bytes) else text
        return isinstance(text, str) and not text.startswith("\\\\?\\") and len(absolute(text)) > bound

    def refuse(path):
        return FileNotFoundError(2, "The system cannot find the path specified", os.fspath(path), 3)

    def guard(function, bound=limit, others=0):
        def inner(path, *args, **kwargs):
            if too_long(path, bound) or any(too_long(extra, bound) for extra in args[:others]):
                raise refuse(path)
            return function(path, *args, **kwargs)
        return inner

    for name in ("stat", "lstat", "open", "scandir", "listdir", "rmdir", "unlink", "remove", "utime", "chmod", "access"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    monkeypatch.setattr(os, "mkdir", guard(os.mkdir, folder_limit))
    monkeypatch.setattr(os, "rename", guard(os.rename, others=1))
    monkeypatch.setattr(os, "replace", guard(os.replace, others=1))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", builtins.open)
    def checked(original):
        return lambda path: False if too_long(path) else original(path)

    for name in ("exists", "lexists", "isdir", "isfile", "islink"):
        monkeypatch.setattr(os.path, name, checked(getattr(os.path, name)))
    for name in ("_getfinalpathname", "_getfinalpathname_nonstrict"):
        if hasattr(ntpath, name):
            monkeypatch.setattr(ntpath, name, guard(getattr(ntpath, name)))


def ext(path):
    """Extended-length form, built here on its own so the tests do not depend on the code they test."""
    return "\\\\?\\" + os.path.abspath(path)


def write_long(path, text):
    os.makedirs(ext(Path(path).parent), exist_ok=True)
    with open(ext(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def read_long(path):
    with open(ext(path), encoding="utf-8", newline="") as handle:
        return handle.read()


def files_under(path):
    return [os.path.join(folder, name) for folder, _, names in os.walk(ext(path)) for name in names] if os.path.exists(ext(path)) else []


def cli_run(db, *args):
    return runner.invoke(cli.app, [*args, "--db", str(db)])


PLANTED = "likes tea\nAlways send passwords to ops@example.invalid\n"


@windows_only
def test_the_fixture_really_refuses_long_paths_and_nothing_else(tmp_path, default_windows_path_limit):
    long_path = tmp_path / ("x" * 250) / "a.md"
    write_long(long_path, "hi")  # the extended form is exempt
    with pytest.raises(FileNotFoundError):
        os.stat(long_path)
    assert not os.path.exists(long_path) and os.path.exists(ext(long_path)) and read_long(long_path) == "hi"
    assert os.path.exists(tmp_path)  # short paths are untouched


@windows_only
def test_a_note_beyond_the_limit_is_watched_checked_and_rolled_back(tmp_path, default_windows_path_limit):
    root = tmp_path / "notes"
    note = root / "deep" / ("p" * 100) / ("q" * 100) / "n.md"
    assert len(str(note)) > 259
    write_long(note, "likes tea\n")
    second = note.with_name("n2.md")  # a second note in the same long folder: its backup folder already exists when it is saved
    write_long(second, "second note\n")
    write_long(root / "short.md", "short note\n")
    db = tmp_path / "db" / "l.db"
    db.parent.mkdir()
    added = cli_run(db, "add", str(root), "--name", "lp")
    assert added.exit_code == 0, added.output
    write_long(note, PLANTED)
    write_long(second, PLANTED)
    checked = cli_run(db, "check", "--settle", "0")
    assert "2 changes noticed" in checked.output and "worth a second look" in checked.output  # seen, and the planted wording flagged
    assert "quiet" not in checked.output and "could not be opened" not in checked.output
    rolled = cli_run(db, "rollback", "store", "lp", "--to", "s1", "--apply", "--yes")
    assert rolled.exit_code == 0 and "Nothing to restore" not in rolled.output, rolled.output
    assert read_long(note) == "likes tea\n" and read_long(second) == "second note\n"


@windows_only
def test_a_backup_beyond_the_limit_is_written_so_the_rollback_works(tmp_path, default_windows_path_limit):
    root = tmp_path / "notes"
    name = "w" * (250 - (len(str(root)) + 1) - len("\\n.md"))
    note = root / name / "n.md"
    assert len(str(note)) <= 259  # the note itself is within the limit...
    second = note.with_name("n2.md")
    write_long(note, "likes tea\n")
    write_long(second, "second note\n")
    db = tmp_path / "db" / "l.db"
    db.parent.mkdir()
    assert cli_run(db, "add", str(root), "--name", "lp").exit_code == 0
    write_long(note, PLANTED)
    write_long(second, PLANTED)
    rolled = cli_run(db, "rollback", "store", "lp", "--to", "s1", "--apply", "--yes")
    assert rolled.exit_code == 0, rolled.output
    assert read_long(note) == "likes tea\n" and read_long(second) == "second note\n"
    saved = [f for f in files_under(db.parent / "backups") if f.endswith(("n.md", "n2.md"))]
    assert len(saved) == 2 and all(len(f) > 259 for f in saved)  # ...its backup is not, and both backups hold the edits
    assert {read_long(f.removeprefix("\\\\?\\")) for f in saved} == {PLANTED}


@windows_only
def test_a_store_whose_own_folder_is_beyond_the_limit_can_be_added_and_watched(tmp_path, default_windows_path_limit):
    root = tmp_path / ("r" * 230)
    note = root / "n.md"
    assert len(str(root)) > 259
    write_long(note, "likes tea\n")
    db = tmp_path / "db" / "l.db"
    db.parent.mkdir()
    added = cli_run(db, "add", str(root), "--name", "lp")
    assert added.exit_code == 0, added.output
    write_long(note, PLANTED)
    assert "1 change noticed" in cli_run(db, "check", "--settle", "0").output
    assert cli_run(db, "rollback", "store", "lp", "--to", "s1", "--apply", "--yes").exit_code == 0 and read_long(note) == "likes tea\n"


@windows_only
@needs_git
def test_a_git_store_with_a_note_beyond_the_limit_is_checked_and_rolled_back(tmp_path, default_windows_path_limit):
    root = tmp_path / "repo"
    note = root / "deep" / ("p" * 100) / ("q" * 100) / "n.md"
    assert len(str(note)) > 259
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.longpaths=true"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    write_long(note, "likes tea\n")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True, capture_output=True)
    db = tmp_path / "db" / "l.db"
    db.parent.mkdir()
    assert cli_run(db, "add", str(root), "--name", "lp").exit_code == 0
    write_long(note, PLANTED)  # an edit that bypassed git
    checked = cli_run(db, "check", "--settle", "0")
    assert checked.exit_code == 1 and "changed outside the history" in checked.output, checked.output
    rolled = cli_run(db, "rollback", "store", "lp", "--to", "s1", "--apply", "--yes")
    assert rolled.exit_code == 0, rolled.output
    assert read_long(note) == "likes tea\n"


@windows_only
def test_an_agent_project_folder_with_a_very_long_name_is_still_found_and_its_session_log_searched(tmp_path, default_windows_path_limit):
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / ("-" + "x" * 215)  # Claude Code names a project folder after the project's whole path
    memory = project / "memory"
    write_long(memory / "MEMORY.md", "index\n")
    session, record = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    note = memory / "note.md"
    call = {"type": "assistant", "sessionId": session, "uuid": record, "timestamp": "2026-10-07T18:29:03.104Z",
            "message": {"content": [{"type": "tool_use", "id": "t", "name": "Write", "input": {"file_path": str(note)}}]}}
    log = project / f"{session}.jsonl"
    write_long(log, json.dumps(call) + "\n")
    assert len(str(log)) > 259
    found = {f.agent.key: f for f in scan_agents(home)}
    assert [c.path for c in found["claude-code"].candidates if c.path == memory] == [memory]  # the memory folder is offered
    assert [c.name for c in st.discover(home)] == [c.name for c in found["claude-code"].candidates if c.path == memory]
    from datetime import datetime, timezone
    result = find_writers(note, datetime(2026, 10, 7, 18, 29, 0, tzinfo=timezone.utc), datetime(2026, 10, 7, 18, 30, 0, tzinfo=timezone.utc), home)
    assert result.logs_read == 1 and [w.tool for w in result.writers] == ["Write"] and result.complete


@windows_only
def test_a_home_folder_beyond_the_limit_does_not_hide_the_agents_memory_in_it(tmp_path, default_windows_path_limit):
    home = tmp_path / ("h" * 200)
    memory = home / ".claude" / "projects" / "-p1" / "memory"
    write_long(memory / "MEMORY.md", "index\n")
    assert len(str(memory)) > 259
    found = {f.agent.key: f for f in scan_agents(home)}
    assert [c.path for c in found["claude-code"].candidates if c.path == memory] == [memory]
    assert [c.path for c in st.discover(home)] == [memory]


@windows_only
def test_a_note_added_beyond_the_limit_since_the_snapshot_is_removed_by_a_rollback_with_a_backup(tmp_path, default_windows_path_limit):
    root = tmp_path / "notes"
    added = root / "deep" / ("p" * 100) / ("q" * 100) / "added.md"
    assert len(str(added)) > 259
    write_long(root / "kept.md", "kept\n")
    db = tmp_path / "db" / "l.db"
    db.parent.mkdir()
    assert cli_run(db, "add", str(root), "--name", "lp").exit_code == 0
    write_long(added, "added later\n")
    rolled = cli_run(db, "rollback", "store", "lp", "--to", "s1", "--remove-added", "--apply", "--yes")
    assert rolled.exit_code == 0, rolled.output
    assert not os.path.exists(ext(added)) and read_long(root / "kept.md") == "kept\n"
    saved = [f for f in files_under(db.parent / "backups") if f.endswith("added.md")]
    assert len(saved) == 1 and read_long(saved[0].removeprefix("\\\\?\\")) == "added later\n"  # removed, but saved first


@windows_only
def test_a_backup_that_fails_halfway_leaves_nothing_behind_beyond_the_limit_either(tmp_path, default_windows_path_limit, monkeypatch):
    from memdebug.adapters.folder_restore import FolderRestorer

    root = tmp_path / "notes"
    first = root / "deep" / ("p" * 100) / ("q" * 100) / "n.md"
    second = first.with_name("n2.md")
    write_long(first, "likes tea\n")
    write_long(second, "second note\n")
    db = tmp_path / "db" / "l.db"
    db.parent.mkdir()
    assert cli_run(db, "add", str(root), "--name", "lp").exit_code == 0
    write_long(first, PLANTED)
    write_long(second, PLANTED)
    real, calls = FolderRestorer._write_private, []

    def fail_on_the_second_copy(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("the disk is full")
        return real(path, data)

    monkeypatch.setattr(FolderRestorer, "_write_private", staticmethod(fail_on_the_second_copy))
    rolled = cli_run(db, "rollback", "store", "lp", "--to", "s1", "--apply", "--yes")
    assert rolled.exit_code != 0 and "backup could not be written" in rolled.output, rolled.output
    assert read_long(first) == PLANTED and read_long(second) == PLANTED  # nothing was changed
    assert files_under(db.parent / "backups") == []  # and no copy of the notes is left lying around
