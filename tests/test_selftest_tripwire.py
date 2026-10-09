"""The self-test's rollback check: its tripwires say what started them, and its control cannot contaminate the protected rollback.

Background: the check once failed on CI with "a program from the repository ran during the rollback: fsmonitor" and passed on a re-run. The control
phase (ordinary git, tripwires armed) and the protected rollback used to share one repository and one set of marker files, so a late leftover from
the control would have looked exactly like a real escape. The check must still catch a real one, so these tests break the protection on purpose.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pytest

import memdebug.selftest as st
from memdebug.adapters import markdown_git as mg

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc, which only Linux has")
needs_ps = pytest.mark.skipif(os.name != "posix" or not shutil.which("ps"), reason="needs a POSIX ps")


def trip(trap, marker, *args, stdin=b"", cwd=None, code=None):
    command = [sys.executable, *(["-c", code] if code else [str(trap)]), str(marker), *args]
    return subprocess.run(command, input=stdin, capture_output=True, cwd=cwd, timeout=60)


def records(marker):
    return [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]


def run_check(check=None):
    try:
        return (check or st.check_rollback_is_safe)()
    except st._SkipError as exc:
        pytest.skip(str(exc))


def load_tripwire(tmp_path):
    """The tripwire script as a module, so its functions can be called directly. Loading is done with bytecode writing off: otherwise Python
    drops a __pycache__ folder beside the script (CI runners do not set PYTHONDONTWRITEBYTECODE), which is no marker but is not nothing either."""
    path = st._write_tripwire(tmp_path / "trap.py")
    spec = importlib.util.spec_from_file_location("tripwire_under_test", path)
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# -- the tripwire -------------------------------------------------------------------------------------------------------------

def test_the_tripwire_records_what_started_it_and_passes_its_input_through(tmp_path):
    trap, marker = st._write_tripwire(tmp_path / "trap.py"), tmp_path / "marker"
    before = datetime.now(timezone.utc)
    done = trip(trap, marker, "fsmonitor", "2", "token", stdin=b"payload", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == b"payload"  # it still behaves like the filter it stands in for
    [record] = records(marker)
    assert {"time", "args", "pid", "ppid", "parent", "grandparent", "cwd", "git_env"} <= set(record)
    assert record["args"] == ["fsmonitor", "2", "token"] and record["pid"] != os.getpid() and isinstance(record["ppid"], int)
    if os.name != "nt":  # a Windows virtual environment's python.exe is a launcher that starts the real interpreter, so the parent is the launcher
        assert record["ppid"] == os.getpid()
    assert before <= datetime.fromisoformat(record["time"]) <= datetime.now(timezone.utc)
    trip(trap, marker, "again", cwd=tmp_path)
    assert [r["args"] for r in records(marker)] == [["fsmonitor", "2", "token"], ["again"]]  # later calls add to it, never replace it


@linux_only
def test_on_linux_the_tripwire_names_its_parent_and_grandparent_command_lines(tmp_path):
    trap, marker = st._write_tripwire(tmp_path / "trap.py"), tmp_path / "marker"
    script = f'"{sys.executable}" "{trap}" "{marker}" x; true'  # the trailing command keeps sh from replacing itself with the tripwire
    subprocess.run(["sh", "-c", script], capture_output=True, timeout=60, check=True)
    [record] = records(marker)
    assert "sh" in record["parent"] and str(trap) in record["parent"]
    assert record["grandparent"]  # this process: the one that started sh


def test_where_the_platform_shows_no_parent_the_record_says_so_and_still_exists(tmp_path):
    trap, marker = st._write_tripwire(tmp_path / "trap.py"), tmp_path / "marker"
    trip(trap, marker, "x", cwd=tmp_path)
    [record] = records(marker)
    assert record["parent"] is None or isinstance(record["parent"], str)  # best effort: a string where it can be read, else None


def test_a_marker_appears_even_when_recording_the_details_fails(tmp_path):
    trap, marker = st._write_tripwire(tmp_path / "trap.py"), tmp_path / "marker"
    code = ("import os, runpy; os.getcwd = lambda: 1 / 0; "  # recording the working folder now fails
            f"runpy.run_path({str(trap)!r}, run_name='__main__')")
    done = trip(trap, marker, "x", code=code)
    assert done.returncode == 0
    [record] = records(marker)
    assert "ZeroDivisionError" in record["recording_failed"] and record["args"] == ["x"]  # the signal matters more than the detail


# -- what a failure says --------------------------------------------------------------------------------------------------------

def test_the_failure_text_carries_the_records_and_stays_printable(tmp_path):
    markers = {"fsmonitor": tmp_path / "m1", "hook": tmp_path / "m2", "diff": tmp_path / "m3", "filter": tmp_path / "m4"}
    markers["fsmonitor"].write_text("".join(f'{{"n": {n}, "note": "\x1b[2J‮"}}\n' for n in range(5)), encoding="utf-8")  # raw escape and bidi characters
    markers["hook"].write_text("", encoding="utf-8")
    markers["diff"].mkdir()  # cannot be read as a file
    report = st._tripwire_report(markers, ["fsmonitor", "hook", "diff"])
    assert "[fsmonitor] 5 call(s):" in report and '"n": 0' in report and '"n": 2' in report and '"n": 3' not in report  # the first three
    assert "\x1b" not in report and "‮" not in report and "\\x1b" in report  # shown as visible escapes
    assert "[hook] 0 call(s): (empty)" in report and "[diff] 1 call(s): (the marker could not be read)" in report


def test_the_git_version_line_names_what_the_program_printed():
    assert st._git_version_line(sys.executable).startswith("git --version: Python 3.")
    assert st._git_version_line("no-such-program-here") == "git --version: unavailable"


# -- two repositories, two sets of markers ------------------------------------------------------------------------------------

@needs_git
def test_the_control_and_the_protected_repositories_share_nothing(tmp_path):
    git = st._need_git()
    trap = st._write_tripwire(tmp_path / "trap.py")
    control = st._hostile_repo(git, tmp_path / "control", trap, "CONTROL")
    protected = st._hostile_repo(git, tmp_path / "protected", trap, "PWNED")
    assert control.repo != protected.repo and set(control.markers) == set(protected.markers) == set(st.TRIPS)
    assert set(control.markers.values()).isdisjoint(protected.markers.values())
    config = {"control": (control.repo / ".git" / "config").read_text(encoding="utf-8"),
              "protected": (protected.repo / ".git" / "config").read_text(encoding="utf-8")}
    assert "CONTROL-" in config["control"] and "PWNED-" not in config["control"]  # each repository's traps write only to its own markers
    assert "PWNED-" in config["protected"] and "CONTROL-" not in config["protected"]
    assert not any(marker.exists() for marker in [*control.markers.values(), *protected.markers.values()])  # building them fires nothing


def with_late_leftover(monkeypatch, control="_run_control"):
    """After the control phase, something it started lands late: a record appears in the control's markers."""
    real = getattr(st, control)

    def run_control(git, hostile):
        armed = real(git, hostile)
        for marker in hostile.markers.values():
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write('{"leftover": true}\n')
        return armed

    monkeypatch.setattr(st, control, run_control)


@needs_git
def test_a_late_leftover_from_the_control_cannot_fail_the_protected_rollback(monkeypatch):
    with_late_leftover(monkeypatch)
    assert run_check().startswith("restored exact bytes in a new commit")


@needs_git
def test_that_leftover_would_have_failed_a_check_whose_phases_share_markers(monkeypatch, tmp_path):
    """The old design, rebuilt on purpose: both phases write to the same marker files. The same leftover now looks like an escape, which is
    why the phases are separate."""
    with_late_leftover(monkeypatch)
    real, shared = st._hostile_repo, tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(st, "_hostile_repo", lambda git, root, trap, prefix, markers_dir=None: real(git, root, trap, "SHARED", markers_dir=shared))
    with pytest.raises(AssertionError, match="ran during the rollback"):
        run_check()


# -- a real escape is still caught, and now explained --------------------------------------------------------------------------

def without_flag(monkeypatch, prefix):
    """Switch one of git's protections off, as a mistake in the product might."""
    real = mg._Git._command

    def command(self, args):
        out = real(self, args)
        for i in range(len(out) - 1):
            if out[i] == "-c" and out[i + 1].startswith(prefix):
                return out[:i] + out[i + 2:]
        raise AssertionError(f"the protection {prefix} is no longer there to remove")

    monkeypatch.setattr(mg._Git, "_command", command)


BROKEN = {"fsmonitor": ("core.fsmonitor=", "fsmonitor", "fsmonitor"), "hooks": ("core.hooksPath=", "hook", "hook-")}


@needs_git
@pytest.mark.parametrize("name", list(BROKEN), ids=list(BROKEN))
def test_a_tripwire_firing_in_the_protected_rollback_is_still_caught_and_explained(monkeypatch, name):
    flag, tripped, first_argument = BROKEN[name]
    without_flag(monkeypatch, flag)
    with pytest.raises(AssertionError) as caught:
        run_check()
    message = str(caught.value)
    assert message.startswith(f"a program from the repository ran during the rollback: {tripped}")
    assert f"[{tripped}]" in message and '"time": "' in message and f'"args": ["{first_argument}' in message  # what started it, and when
    assert '"pid"' in message and '"parent"' in message and '"cwd"' in message
    assert "git --version: " in message and "not a leftover from it" in message
    assert "PWNED-" not in message.split("What started it")[0]  # the headline names the trap, not a file


@needs_git
@linux_only
def test_on_linux_a_caught_escape_names_the_process_that_ran_it(monkeypatch):
    without_flag(monkeypatch, "core.hooksPath=")
    with pytest.raises(AssertionError) as caught:
        run_check()
    assert re.search(r'"parent": "[^"]+"', str(caught.value))  # the hook script's shell, read from /proc


# -- finding a process's parent: the /proc path (Linux) and the ps path (macOS and the BSDs), called directly ----------------------------

def test_importing_the_tripwire_runs_nothing(tmp_path):
    module = load_tripwire(tmp_path)
    assert callable(module.from_proc) and callable(module.from_ps) and callable(module.parse_ps)
    assert list(tmp_path.iterdir()) == [tmp_path / "trap.py"]  # no marker was written by merely loading it


PS_OUTPUT = {
    "macos": ("  501 /usr/bin/git --no-pager log", ("/usr/bin/git --no-pager log", 501)),
    "minimal": ("1 sh", ("sh", 1)),
    "spaces": (" 4242   python  -c  x \n", ("python  -c  x", 4242)),  # spaces inside the command are kept
    "empty": ("", (None, None)),
    "blank": ("   \n", (None, None)),
    "pid-only": ("123", (None, None)),
    "trailing": ("12 ", (None, None)),
    "not-a-number": ("abc sh", (None, None)),
}


@pytest.mark.parametrize("text, expected", list(PS_OUTPUT.values()), ids=list(PS_OUTPUT))
def test_what_ps_prints_is_read_the_same_way_on_every_platform(tmp_path, text, expected):
    assert load_tripwire(tmp_path).parse_ps(text) == expected


@needs_ps
def test_the_ps_lookup_finds_this_process_and_its_parent(tmp_path):
    command, parent = load_tripwire(tmp_path).from_ps(os.getpid())
    assert parent == os.getppid() and isinstance(command, str) and command  # what macOS would report, asked of the real ps


@needs_ps
@linux_only
def test_on_linux_the_ps_lookup_agrees_with_the_proc_lookup(tmp_path):
    module = load_tripwire(tmp_path)
    from_ps, from_proc = module.from_ps(os.getpid()), module.from_proc(os.getpid())
    assert from_ps[1] == from_proc[1] == os.getppid()
    assert from_ps[0].split()[0] == from_proc[0].split()[0]  # the same program, spelled the way each source spells it


@needs_ps
def test_ps_has_nothing_to_say_about_a_process_that_is_gone(tmp_path):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=60)
    assert load_tripwire(tmp_path).from_ps(gone.pid) == (None, None)


@needs_ps
def test_a_missing_or_stuck_ps_gives_nothing_instead_of_an_error(tmp_path, monkeypatch):
    module = load_tripwire(tmp_path)
    for failure in (FileNotFoundError("no ps"), subprocess.TimeoutExpired("ps", 5)):
        def broken(*args, _failure=failure, **kwargs):
            raise _failure
        monkeypatch.setattr(module.subprocess, "run", broken)
        assert module.from_ps(os.getpid()) == (None, None)


@pytest.mark.skipif(os.name == "posix", reason="shows the Windows answer; POSIX asks ps")
def test_without_ps_or_proc_the_lookup_says_unavailable(tmp_path):
    module = load_tripwire(tmp_path)
    assert module.from_ps(os.getpid()) == (None, None) and module.process(os.getpid()) == (None, None)


def test_a_long_command_line_is_cut_so_it_cannot_crowd_out_the_rest_of_the_record(tmp_path):
    module = load_tripwire(tmp_path)
    assert module.clip(None) is None and module.clip("short") == "short"
    cut = module.clip("x" * 5000)
    assert len(cut) == 303 and cut.endswith("...") and module.clip("y" * 300) == "y" * 300


def test_the_lookup_uses_proc_when_it_has_an_answer_and_ps_only_when_it_has_none(tmp_path):
    module = load_tripwire(tmp_path)
    module.from_proc = lambda pid: ("from proc", 1)
    module.from_ps = lambda pid: pytest.fail("ps must not be asked when /proc answered")
    assert module.process(5) == ("from proc", 1)
    module.from_proc = lambda pid: (None, None)
    module.from_ps = lambda pid: ("from ps", 2)
    assert module.process(5) == ("from ps", 2)  # the macOS path: /proc does not exist there


# -- the config check (hostile repository config cannot run programs): the same separation ------------------------------------------

CONFIG_OK = "a repository config that runs programs did not run any"


@needs_git
def test_the_config_control_and_protected_repositories_share_nothing(tmp_path):
    git = st._need_git()
    trap = st._write_tripwire(tmp_path / "trap.py")
    control = st._config_trap_repo(git, tmp_path / "control", trap, "CONTROL")
    protected = st._config_trap_repo(git, tmp_path / "protected", trap, "PWNED")
    assert control.repo != protected.repo and set(control.markers) == set(protected.markers) == set(st.CONFIG_TRIPS)
    assert not set(control.markers.values()) & set(protected.markers.values())
    control_config = (control.repo / ".git" / "config").read_text(encoding="utf-8")
    protected_config = (protected.repo / ".git" / "config").read_text(encoding="utf-8")
    assert "CONTROL-diff" in control_config and "PWNED-" not in control_config
    assert "PWNED-diff" in protected_config and "CONTROL-" not in protected_config
    assert not any(m.exists() for m in [*control.markers.values(), *protected.markers.values()])


@needs_git
def test_the_check_passes_and_says_which_settings_the_control_proved():
    message = run_check(st.check_hostile_repo_config_cannot_run_programs)
    assert message.startswith(CONFIG_OK) and "control: ordinary git ran the" in message and message.endswith("memdebug's git did not")


@needs_git
def test_a_late_leftover_from_the_config_control_cannot_fail_the_protected_run(monkeypatch):
    with_late_leftover(monkeypatch, "_run_config_control")
    assert run_check(st.check_hostile_repo_config_cannot_run_programs).startswith(CONFIG_OK)


@needs_git
def test_that_leftover_would_have_failed_a_config_check_whose_phases_share_a_marker(monkeypatch, tmp_path):
    with_late_leftover(monkeypatch, "_run_config_control")
    real, shared = st._config_trap_repo, tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(st, "_config_trap_repo", lambda git, root, trap, prefix, markers_dir=None: real(git, root, trap, "SHARED", markers_dir=shared))
    with pytest.raises(AssertionError, match="a program named in the repository's config was executed"):
        run_check(st.check_hostile_repo_config_cannot_run_programs)


@needs_git
def test_a_program_run_by_the_history_reader_is_still_caught_and_explained(monkeypatch):
    """The history reader asks git for `--raw` and passes `--no-ext-diff`, so git never runs a diff program for it. Break both on purpose, as a
    change that asked for patches without the flag would, and the check must see the program run."""
    real = mg._Git.spawn

    def spawn(self, args, **kwargs):
        return real(self, ["-p" if a == "--raw" else "--ext-diff" if a == "--no-ext-diff" else a for a in args], **kwargs)

    monkeypatch.setattr(mg._Git, "spawn", spawn)
    with pytest.raises(AssertionError) as caught:
        run_check(st.check_hostile_repo_config_cannot_run_programs)
    message = str(caught.value)
    assert message.startswith("a program named in the repository's config was executed. What started it: [diff]")
    assert '"time": "' in message and '"args": ["diff"' in message and '"cwd"' in message
    assert "git --version: " in message and "not a leftover from it" in message


def without_fsmonitor_protection(monkeypatch):
    """Break the protection on purpose: the wrapper no longer switches the file system monitor off."""
    real = mg._Git._command

    def command(self, args):
        built = real(self, args)
        at = built.index("core.fsmonitor=false")
        return built[:at - 1] + built[at + 1:]  # drop the "-c" and its value

    monkeypatch.setattr(mg._Git, "_command", command)


@needs_git
def test_losing_the_fsmonitor_protection_is_caught_and_explained(monkeypatch):
    without_fsmonitor_protection(monkeypatch)
    with pytest.raises(AssertionError) as caught:
        run_check(st.check_hostile_repo_config_cannot_run_programs)
    message = str(caught.value)
    assert message.startswith("a program named in the repository's config was executed. What started it: [fsmonitor]")
    assert '"args": ["fsmonitor"' in message and "git --version: " in message


@needs_git
def test_the_old_check_would_not_have_noticed_that_loss(monkeypatch, tmp_path):
    """Why the check now asks for a status and a patch: reading history, files and objects alone never makes git consult the file system monitor,
    so with the protection gone the reads of the old check still left no marker."""
    git = st._need_git()
    trap = st._write_tripwire(tmp_path / "trap.py")
    protected = st._config_trap_repo(git, tmp_path / "protected", trap, "PWNED")
    without_fsmonitor_protection(monkeypatch)
    adapter = mg.MarkdownGitAdapter(protected.repo, store="selftest")
    adapter.read_history(100)
    adapter.history("a.md")
    adapter.list_memories({"store": "selftest"})
    assert not any(marker.exists() for marker in protected.markers.values())


@needs_git
def test_without_a_control_that_fires_the_check_says_skip_never_pass(monkeypatch):
    monkeypatch.setattr(st, "_run_config_control", lambda git, hostile: [])
    with pytest.raises(st._SkipError, match="protection is unproven"):
        st.check_hostile_repo_config_cannot_run_programs()


@needs_git
def test_a_control_that_only_fired_the_pager_or_editor_settings_proves_nothing(monkeypatch):
    monkeypatch.setattr(st, "_run_config_control", lambda git, hostile: ["other"])
    with pytest.raises(st._SkipError, match="protection is unproven"):
        st.check_hostile_repo_config_cannot_run_programs()
