"""The self-test in a stand-alone build, where `sys.executable` is memdebug itself and not a Python that can run a script.

The first stand-alone build showed two problems. The tripwire checks ("hostile repository config", "rollback is safe") could not start their tripwire and
said SKIP, and the "hung process tree is killed" check PASSED for nothing: it started `memdebug.exe -c ...`, which exits at once on a usage error, so
there was no stalled process to kill. In a stand-alone build the checks now start `memdebug selftest-helper ...`, a fixed set of jobs that is not a way to
run code. A launcher script stands in for the stand-alone program here: it starts memdebug under the Python that runs the tests.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import memdebug.selftest as st

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")


@pytest.fixture
def launcher(tmp_path):
    """A program that behaves like the stand-alone build: `launcher selftest-helper ...` runs memdebug's own command."""
    if os.name == "nt":
        path = tmp_path / "memdebug-standalone.cmd"
        path.write_text(f'@"{sys.executable}" -m memdebug %*\r\n', encoding="utf-8")
    else:
        path = tmp_path / "memdebug-standalone"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m memdebug "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return path


@pytest.fixture
def standalone(monkeypatch, launcher):
    """Make the self-test believe it runs in a stand-alone build whose program is the launcher."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher))
    return launcher


def run(check):
    try:
        return check()
    except st._SkipError as exc:
        pytest.skip(str(exc))


# -- the command a hostile repository is made to run ----------------------------------------------------------------------------------

def test_with_a_python_the_tripwire_script_is_what_runs(tmp_path):
    command = st._tripwire_command(tmp_path / "trap.py", tmp_path / "marker", "diff")
    assert command.startswith(f'"{Path(sys.executable).as_posix()}" "{(tmp_path / "trap.py").as_posix()}" "{(tmp_path / "marker").as_posix()}" "diff"')
    assert "selftest-helper" not in command


def test_in_a_standalone_build_the_helper_runs_instead_and_quotes_are_the_callers(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    double = st._tripwire_command(tmp_path / "trap.py", tmp_path / "marker", "hook-pre-commit")
    single = st._tripwire_command(tmp_path / "trap.py", tmp_path / "marker", "hook-pre-commit", quote="'")
    assert double.endswith(f'selftest-helper" "tripwire" "{(tmp_path / "marker").as_posix()}" "hook-pre-commit"')
    assert single.endswith(f"'selftest-helper' 'tripwire' '{(tmp_path / 'marker').as_posix()}' 'hook-pre-commit'") and '"' not in single
    assert "trap.py" not in double  # the script is not needed, and not run


# -- the helper is a fixed set of jobs, not a way to run code ------------------------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["-c", "open('ran', 'w').close()"], ["exec", "open('ran', 'w').close()"], ["tripwire"], ["tripwire", "only-a-marker"],
                                  ["Tripwire", "m", "n"]],  # the payloads leave a file behind if they run, which the test looks for
                         ids=["nothing", "dash-c", "exec", "no-args", "no-name", "wrong-case"])
def test_anything_but_a_fixed_job_is_refused_and_runs_nothing(args, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert st.run_helper(list(args)) == 2
    assert list(tmp_path.iterdir()) == []


def test_the_helper_command_is_hidden_from_the_help_but_refuses_through_the_command_line(tmp_path):
    shown = subprocess.run([sys.executable, "-m", "memdebug", "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert "selftest-helper" not in shown.stdout
    refused = subprocess.run([sys.executable, "-m", "memdebug", "selftest-helper", "-c", "print(1)"], capture_output=True, text=True, cwd=tmp_path)
    assert refused.returncode == 2 and refused.stdout == ""


def test_the_helper_records_what_the_script_records_and_passes_its_input_through(tmp_path):
    trap = st._write_tripwire(tmp_path / "trap.py")
    by_script, by_helper = tmp_path / "by-script", tmp_path / "by-helper"
    one = subprocess.run([sys.executable, str(trap), str(by_script), "diff", "2", "token"], input=b"patch text", capture_output=True, timeout=60)
    two = subprocess.run([sys.executable, "-m", "memdebug", "selftest-helper", "tripwire", str(by_helper), "diff", "2", "token"], input=b"patch text",
                         capture_output=True, timeout=60)
    assert one.stdout == two.stdout == b"patch text" and one.returncode == two.returncode == 0
    a, b = (json.loads(path.read_text(encoding="utf-8")) for path in (by_script, by_helper))  # one record each
    assert a["args"] == b["args"] == ["diff", "2", "token"] and set(a) == set(b)


# -- the checks, end to end, in a simulated stand-alone build ------------------------------------------------------------------------

def test_the_stalled_process_check_really_starts_a_stalled_process(standalone):
    started = time.monotonic()
    message = run(st.check_hung_process_tree_is_killed)
    assert message.startswith("a stalled process and its child were stopped after")
    assert time.monotonic() - started > 0.8  # a program that exits at once (as a usage error would) is not a stalled process; this ran for the timeout


def test_that_check_would_have_passed_for_nothing_if_the_helper_did_not_start_it(standalone, monkeypatch):
    """The false PASS the first stand-alone build showed: a command that exits at once makes the check pass although nothing was killed. The check
    cannot see that on its own, which is why the stand-alone path starts a helper that is known to stall, and why the test above measures time."""
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **k: real([sys.executable, "-c", "pass"], *a, **k))
    assert run(st.check_hung_process_tree_is_killed).startswith("a stalled process")  # PASS, with nothing stalled and nothing killed


@needs_git
def test_the_tripwire_checks_run_and_prove_something_in_a_standalone_build(standalone):
    if os.name == "nt":
        pytest.skip("git for Windows runs its helper programs through sh, which cannot start a .cmd launcher; the real stand-alone build is run by CI")
    assert "control: ordinary git ran" in run(st.check_hostile_repo_config_cannot_run_programs)
    assert run(st.check_rollback_is_safe).startswith("restored exact bytes in a new commit")
