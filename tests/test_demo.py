"""`memdebug demo` must work for a stranger and must never touch anything outside its own folder."""
import hashlib
import os
import shutil
import stat

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.demo as demo
from memdebug.errors import MemdebugError
from memdebug.ledger import Ledger
from memdebug.models import Op

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
runner = CliRunner()


def tree_digest(root):
    digest = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def test_the_demo_tells_the_whole_story_with_the_real_code_paths(tmp_path):
    lines = []
    result = demo.run_demo(tmp_path / "work", lines.append)
    text = "\n".join(lines)
    assert result.outside_edit_seen and result.restored and result.ledger_ok and result.tamper_detected
    assert "changed OUTSIDE the history" in text and "send them to ops@example.invalid" in text
    assert "saved first, not lost" in text and "Recorded in the ledger as rollback:r1" in text
    assert "PROBLEM found" in text and "the ledger: intact" in text
    assert (result.repo / "prefs.md").read_bytes() == demo.GOOD_PREFS.encode()
    assert (result.repo / "people.md").read_bytes() == demo.PEOPLE_UPDATED.encode()  # the legitimate change was not undone


def test_what_the_demo_records_is_what_it_says_it_recorded(tmp_path):
    result = demo.run_demo(tmp_path / "work", lambda line: None)
    ledger = Ledger(result.ledger_path)
    ops = [(e.event.op, e.event.memory_id) for e in ledger.entries()]
    assert ledger.verify().ok
    assert (Op.EXTERNAL, "prefs.md") in ops and (Op.UPDATE, "people.md") in ops and (Op.ROLLBACK, "rollback:r1") in ops
    external = [e.event for e in ledger.entries() if e.event.op == Op.EXTERNAL]
    assert len(external) == 1 and "ops@example.invalid" in external[0].after  # only the attack is flagged, not the rollback
    assert result.backup_ref and result.backup_ref.startswith("refs/memdebug/backups/")


def test_the_demo_never_touches_anything_outside_its_folder(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "mine.md").write_text("my own memory\n", encoding="utf-8")
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "note.md").write_text("keep me\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    before = {"elsewhere": tree_digest(elsewhere), "sibling": tree_digest(sibling)}
    demo.run_demo(tmp_path / "work", lambda line: None)
    assert {"elsewhere": tree_digest(elsewhere), "sibling": tree_digest(sibling)} == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["elsewhere", "sibling", "work"]


def test_the_demo_output_is_plain_ascii_so_any_console_can_show_it(tmp_path):
    lines = []
    demo.run_demo(tmp_path / "work", lines.append)
    assert all(line.isascii() for line in lines if "folder:" not in line)


@pytest.mark.parametrize("make", ["file", "full"])
def test_a_folder_that_already_has_something_in_it_is_refused(tmp_path, make):
    target = tmp_path / "taken"
    if make == "file":
        target.write_text("a file, not a folder", encoding="utf-8")
    else:
        target.mkdir()
        (target / "precious.md").write_text("do not mix with this\n", encoding="utf-8")
    before = tree_digest(tmp_path)
    with pytest.raises(MemdebugError, match="not empty"):
        demo.prepare_folder(target)
    assert tree_digest(tmp_path) == before


def test_a_new_or_empty_folder_is_accepted_and_is_not_treated_as_temporary(tmp_path):
    new = tmp_path / "does" / "not" / "exist"
    path, temporary = demo.prepare_folder(new)
    assert path.is_dir() and temporary is False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert demo.prepare_folder(empty) == (empty.resolve(), False)


def test_removing_the_folder_copes_with_read_only_files(tmp_path):
    folder = tmp_path / "gone"
    (folder / "objects").mkdir(parents=True)
    locked = folder / "objects" / "pack"
    locked.write_text("git marks these read-only", encoding="utf-8")
    locked.chmod(stat.S_IREAD)
    demo.remove_folder(folder)
    assert not folder.exists()


def test_the_command_runs_removes_its_temporary_folder_and_says_so():
    result = runner.invoke(cli.app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "changed OUTSIDE the history" in result.output and "The throwaway folder was removed." in result.output
    folder = next(line.split("folder:", 1)[1].strip() for line in result.output.splitlines() if "folder:" in line)
    assert not os.path.exists(folder)


def test_keep_leaves_the_folder_and_a_ledger_that_the_other_commands_can_read():
    result = runner.invoke(cli.app, ["demo", "--keep"])
    assert result.exit_code == 0, result.output
    folder = next(line.split("folder:", 1)[1].strip() for line in result.output.splitlines() if "folder:" in line)
    try:
        assert "The demo folder was kept" in result.output
        timeline = runner.invoke(cli.app, ["timeline", "--db", os.path.join(folder, "ledger.db")])
        assert "ROLLBACK" in timeline.output and "EXTERNAL" in timeline.output
        assert runner.invoke(cli.app, ["verify", "--db", os.path.join(folder, "ledger.db")]).exit_code == 0
    finally:
        demo.remove_folder(demo.Path(folder))


def test_dir_uses_the_folder_you_name_and_keeps_it(tmp_path):
    target = tmp_path / "chosen"
    result = runner.invoke(cli.app, ["demo", "--dir", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / "ledger.db").is_file() and "The demo folder was kept" in result.output


def test_the_command_refuses_a_busy_folder_cleanly(tmp_path):
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    result = runner.invoke(cli.app, ["demo", "--dir", str(tmp_path)])
    assert result.exit_code == 2 and "not empty" in result.output and "Traceback" not in result.output


def test_without_git_the_demo_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(demo, "find_git", lambda: None)
    result = runner.invoke(cli.app, ["demo"])
    assert result.exit_code == 2 and "git was not found" in result.output


def test_the_demo_points_a_newcomer_at_the_guided_setup_not_an_advanced_command(tmp_path):
    result = runner.invoke(cli.app, ["demo", "--dir", str(tmp_path / "d")])
    assert result.exit_code == 0, result.output
    last = "\n".join(result.output.strip().splitlines()[-3:])
    assert "memdebug setup" in last and "memdebug agents" in last
    assert "snapshot markdown" not in result.output
    assert result.output.isascii()  # any console can show it
