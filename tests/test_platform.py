"""Behaviour that differs by platform, written so it runs (and must pass) on every platform."""
import os
import subprocess
from types import SimpleNamespace

import pytest

from memdebug import paths
from memdebug.adapters import markdown_git as mg
from memdebug.adapters.markdown_git import _bad_component, _is_reparse_point, _valid_relpath, find_git
from memdebug.errors import AdapterError
from memdebug.textsafe import console_safe

SUFFIX = (".md",)


# -- finding git without trusting the current folder -----------------------------------------------

def make_fake_git(folder):
    name = "git.exe" if os.name == "nt" else "git"
    path = folder / name
    path.write_bytes(b"#!/bin/sh\necho planted\n")
    path.chmod(0o755)
    return path


def test_a_git_planted_in_the_current_folder_is_never_chosen(tmp_path, monkeypatch):
    make_fake_git(tmp_path)
    monkeypatch.chdir(tmp_path)
    for path_value in [".", "", os.pathsep, f".{os.pathsep}", f"{os.pathsep}.{os.pathsep}", "relative/dir"]:
        monkeypatch.setenv("PATH", path_value)
        assert find_git() is None, repr(path_value)


def test_an_absolute_path_entry_is_used(tmp_path, monkeypatch):
    fake = make_fake_git(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["relative", str(tmp_path)]))
    assert find_git() == str(fake)


def test_the_git_path_must_be_absolute_and_a_real_file(tmp_path):
    with pytest.raises(AdapterError):
        mg.MarkdownGitAdapter(tmp_path, git_path="git")
    with pytest.raises(AdapterError):
        mg.MarkdownGitAdapter(tmp_path, git_path=str(tmp_path / "nothing"))


# -- path rules ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "NUL.md", "con.md", "Aux/x.md", "dir/COM1.md", "lpt9.txt.md", "PRN.md", "conin$.md",   # Windows devices
    "notes:hidden.md", "C:evil.md",                                                          # streams, drive letters
    "a.md.", "dir /x.md", "dir./x.md", "trailing /a.md",                                     # trailing dot or space
    ".GIT/x.md", ".git/x.md", "GIT~1/x.md", "git~2/x.md",                                    # the git folder
    "a<b>.md", "a|b.md", "a*b.md", "a?b.md", 'q"uote.md', "back\\slash.md",                  # reserved characters
    "../x.md", "a/../b.md", "a//b.md", "./a.md", "/abs.md", "",                              # traversal
    "new\nline.md", "esc\x1bseq.md", "bidi\u202e.md", "zero\u200bwidth.md",                  # control characters
    "x" * 300 + ".md",
])
def test_dangerous_paths_are_rejected_everywhere(path):
    assert _valid_relpath(path, SUFFIX) is None


@pytest.mark.parametrize("path", [
    "ok.md", "people/Ann.md", "caf\u00e9 notes.md", "-rf.md", "a.b.c.md", "CONSOLE.md", "auxiliary/x.md",
    "my-con.md", "com10.md", "Notes/2026-10-04.MD",
])
def test_normal_paths_are_accepted(path):
    assert _valid_relpath(path, SUFFIX) == path


def test_look_alike_names_are_judged_per_component():
    assert not _bad_component("readme") and _bad_component("NUL") and _bad_component("nul.tar.gz")
    assert not _bad_component("null") and not _bad_component("gitignore")


def test_reparse_points_are_detected_from_the_windows_attribute():
    assert _is_reparse_point(SimpleNamespace(st_file_attributes=0x400))
    assert _is_reparse_point(SimpleNamespace(st_file_attributes=0x420))
    assert not _is_reparse_point(SimpleNamespace(st_file_attributes=0x20))
    assert not _is_reparse_point(SimpleNamespace())  # POSIX stat results have no such field


def test_folders_with_unsafe_or_look_alike_names_are_reported_not_silently_dropped(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "ok.md").write_text("x")
    for name in ("aux", ".GIT", "trail "):
        try:
            (repo / name).mkdir()
            (repo / name / "hidden.md").write_text("y")
        except OSError:
            pytest.skip("this filesystem cannot create that folder name")
    live = mg.MarkdownGitAdapter(repo, store="r").list_memories({"store": "r"})
    assert [m.id for m in live.memories] == ["ok.md"]
    assert not live.complete and len(live.warnings) >= 3


def test_the_real_git_folder_is_skipped_silently(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / ".git" / "info" / "x.md").write_text("not a memory")
    (repo / "ok.md").write_text("x")
    live = mg.MarkdownGitAdapter(repo, store="r").list_memories({"store": "r"})
    assert [m.id for m in live.memories] == ["ok.md"] and live.complete and live.warnings == []


# -- printing ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("encoding", ["cp1252", "cp437", "ascii", "latin-1"])
def test_console_text_is_always_encodable(encoding):
    shown = console_safe("\u65e5\u672c\u8a9e \U0001f600 na\u00efve", encoding)
    shown.encode(encoding)
    assert "\u65e5" not in shown and "\\u65e5" in shown


def test_console_safe_keeps_text_the_console_can_show():
    assert console_safe("caf\u00e9", "cp1252") == "caf\u00e9"
    assert console_safe("caf\u00e9", "utf-8") == "caf\u00e9"
    assert "\\" in console_safe("x\U0001f600", "no-such-encoding")  # an unknown encoding must not crash


# -- default ledger location -----------------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="the Windows branch is exercised by CI on Windows")
def test_default_ledger_is_in_a_per_user_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.default_ledger_path() == tmp_path / "memdebug" / "ledger.db"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.default_ledger_path().parts[-3:] == ("share", "memdebug", "ledger.db")


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_default_ledger_uses_local_app_data_on_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.default_ledger_path() == tmp_path / "memdebug" / "ledger.db"


def test_the_default_ledger_is_never_in_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tmp_path not in paths.default_ledger_path().parents


# -- the selftest ----------------------------------------------------------------------------------------------------

def test_selftest_has_no_failures_here():
    from memdebug.selftest import run_all

    results = run_all()
    failures = [r for r in results if r[1] == "FAIL"]
    assert failures == [], failures
    assert {r[1] for r in results} <= {"PASS", "SKIP", "INFO", "FAIL"}
    assert [r for r in results if r[1] == "PASS"], "nothing was actually checked"


def test_product_code_never_relies_on_the_platforms_default_text_encoding():
    """Windows defaults to a legacy code page. Text written or read without an explicit encoding
    breaks on names and memories outside it (selftest once wrote a script containing a user folder)."""
    import pathlib
    import re
    source = pathlib.Path(mg.__file__).resolve().parents[1]
    offenders = []
    for path in source.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.(write_text|read_text)\(", line) and "encoding=" not in line:
                offenders.append(f"{path.name}:{number}")
            if re.search(r"(?<![\w.])open\(", line) and "encoding=" not in line and not re.search(r"""["'][rwa]?b""", line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], offenders
