"""A store that could not be read in full is never reported as quiet.

Before, `memdebug check` said "quiet, nothing new" with exit code 0 when a note could not be read (only a warning after the summary said so), and a
rollback to an incomplete snapshot said "every file in scope already matches the snapshot". Both claimed more than memdebug had seen. The note that
cannot be read here is a folder that is a link (a symbolic link, or a junction on Windows), which memdebug never follows and reports, one of several real ways a
listing becomes incomplete.
"""
import os
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
from memdebug.models import Op
from memdebug.monitor import CheckSummary, StoreResult, describe, watch
from memdebug.stores import StoreConfig
from test_backups import link_dir

runner = CliRunner()


def run(db, *args):
    return runner.invoke(cli.app, [*args, "--db", str(db)])


def drop_link(path):
    os.rmdir(path) if os.name == "nt" else os.unlink(path)  # a junction is removed as a folder; neither removes what it points at


def put(folder, name, data):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


@pytest.fixture
def small(tmp_path):
    """A store whose notes can all be read."""
    notes = tmp_path / "notes"
    put(notes, "prefs.md", "likes tea\n")
    db = tmp_path / "ledger.db"
    assert run(db, "add", str(notes), "--name", "notes").exit_code == 0
    return notes, db


@pytest.fixture
def partial(tmp_path):
    """A store with a folder that is a link, which is not followed, so every listing of it is incomplete."""
    notes = tmp_path / "notes"
    put(notes, "prefs.md", "likes tea\n")
    put(tmp_path / "elsewhere", "other.md", "not part of this store\n")
    link_dir(notes / "linked", tmp_path / "elsewhere")
    db = tmp_path / "ledger.db"
    assert run(db, "add", str(notes), "--name", "notes").exit_code == 0
    return notes, db


# -- check --------------------------------------------------------------------------------------------------------------

def test_a_store_read_in_full_is_still_quiet_with_exit_code_0(small):
    _, db = small
    result = run(db, "check", "--settle", "0")
    assert result.exit_code == 0 and "quiet, nothing new" in result.output and "clean bill of health" not in result.output


def test_a_store_that_could_not_be_read_in_full_is_not_called_quiet_and_exits_1(partial):
    _, db = partial
    result = run(db, "check", "--settle", "0")
    assert result.exit_code == 1, result.output
    assert "quiet, nothing new" not in result.output
    assert "NOT READ IN FULL" in result.output and "clean bill of health" in result.output


def test_a_change_in_a_store_not_read_in_full_says_there_may_be_more(partial):
    notes, db = partial
    put(notes, "prefs.md", "likes coffee\n")
    result = run(db, "check", "--settle", "0")
    assert result.exit_code == 1 and "1 change noticed" in result.output and "so there may be more" in result.output


def test_the_state_comes_back_to_quiet_once_every_note_can_be_read(partial):
    notes, db = partial
    assert run(db, "check", "--settle", "0").exit_code == 1
    drop_link(notes / "linked")
    again = run(db, "check", "--settle", "0")
    assert again.exit_code == 0 and "quiet, nothing new" in again.output and "NOT READ IN FULL" not in again.output


def test_the_exit_codes_put_a_store_not_read_in_full_with_the_ones_that_need_a_look():
    store = StoreConfig(name="notes", kind="folder", path="/x")
    assert CheckSummary([StoreResult(store)], True).exit_code == 0
    partial = CheckSummary([StoreResult(store, incomplete=True)], True)
    assert partial.exit_code == 1 and partial.exit_code_for(False) == 1 and partial.exit_code_for(True) == 1
    both = CheckSummary([StoreResult(store, incomplete=True), StoreResult(store, error="gone")], True)
    assert both.exit_code == 1  # still one of the two: the store that could not be opened is reported by its own line
    assert CheckSummary([StoreResult(store, error="gone")], True).exit_code == 2


def test_the_describe_line_never_says_quiet_for_an_incomplete_store():
    store = StoreConfig(name="notes", kind="folder", path="/x")
    assert "quiet" not in describe(StoreResult(store, incomplete=True))
    assert "quiet" in describe(StoreResult(store))
    assert "may be more" in describe(StoreResult(store, changes=[(Op.UPDATE, "a.md")], incomplete=True))


# -- watch --------------------------------------------------------------------------------------------------------------

def test_watch_says_a_store_is_not_read_in_full_once_not_at_every_look(partial):
    notes, db = partial
    from memdebug.ledger import Ledger
    from memdebug.stores import load_registry

    registry = load_registry(db.parent / "stores.json")
    said, rang = [], []
    watch(registry, Ledger(db), every=5, say=said.append, ring=lambda: rang.append(1), cycles=3, sleep=lambda s: None, settle=0)
    assert sum("NOT READ IN FULL" in line for line in said) == 1 and len(rang) == 1


def test_watch_says_it_again_if_the_store_becomes_unreadable_once_more(partial):
    notes, db = partial
    from memdebug.ledger import Ledger
    from memdebug.stores import load_registry

    registry = load_registry(db.parent / "stores.json")
    ledger = Ledger(db)
    said = []
    steps = iter([lambda: drop_link(notes / "linked"), lambda: link_dir(notes / "linked", notes.parent / "elsewhere"), lambda: None])

    def sleep(seconds):
        next(steps)()

    watch(registry, ledger, every=5, say=said.append, cycles=4, sleep=sleep, settle=0)
    assert sum("NOT READ IN FULL" in line for line in said) == 2


# -- rollback -----------------------------------------------------------------------------------------------------------

def test_a_rollback_to_an_incomplete_snapshot_does_not_claim_everything_matches(partial):
    _, db = partial
    result = run(db, "rollback", "store", "notes", "--to", "s1")
    assert result.exit_code == 0
    assert "every file in scope already matches" not in result.output
    assert "snapshot is incomplete" in result.output and "cannot vouch for the rest" in result.output


def test_a_rollback_to_a_complete_snapshot_still_says_everything_matches(small):
    _, db = small
    result = run(db, "rollback", "store", "notes", "--to", "s1")
    assert result.exit_code == 0 and "every file in scope already matches the snapshot" in result.output


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_a_git_rollback_to_an_incomplete_snapshot_does_not_claim_everything_matches(tmp_path):
    repo = tmp_path / "repo"
    put(repo, "prefs.md", "likes tea\n")
    put(tmp_path / "elsewhere", "other.md", "not part of this store\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "one"]):
        assert subprocess.run(["git", *args], cwd=repo, capture_output=True).returncode == 0
    link_dir(repo / "linked", tmp_path / "elsewhere")  # untracked, and never followed: the working-tree listing is incomplete
    db = tmp_path / "ledger.db"
    assert run(db, "add", str(repo), "--name", "notes").exit_code == 0
    result = run(db, "rollback", "store", "notes", "--to", "s1")
    assert result.exit_code == 0, result.output
    assert "every file in scope already matches" not in result.output and "snapshot is incomplete" in result.output
