"""Starting `memdebug watch` at login: the plan for each system, what applying it writes and runs, and that nothing happens without --apply.
No real task, service or agent is ever created here: commands are recorded, not run."""
import os
import plistlib
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import memdebug.autostart as au
import memdebug.cli as cli
import memdebug.stores as st
from memdebug.errors import MemdebugError

runner = CliRunner()
Path0 = Path("/tmp/x")  # only ever used to build plans, never touched
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")
TOOLS = {"schtasks": "C:\\Win\\System32\\schtasks.exe", "powershell": "C:\\Win\\System32\\powershell.exe", "systemctl": "/usr/bin/systemctl",
         "launchctl": "/bin/launchctl"}
ARGS = ["watch", "--every", "60", "--no-bell", "--log", "/data/watch.log"]


def plan(system, command=None, args=None, tmp=None, **kw):
    tmp = tmp or Path0
    return au.make_plan(system, command or ["/usr/bin/python3", "-m", "memdebug"], args or ARGS, data_dir=tmp / "data", home=tmp / "home",
                        uid=501, tools=TOOLS, **kw)


class Recorder:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, args):
        self.calls.append(tuple(args))
        return (1, "boom") if self.fail_on and self.fail_on in " ".join(args) else (0, "")



# -- the plans ---------------------------------------------------------------------------------------------------------------------

def test_the_windows_plan_is_a_launcher_script_and_a_per_user_logon_task_with_every_program_by_absolute_path(tmp_path):
    p = plan("windows", ["C:\\Program Files\\Py thon\\python.exe", "-m", "memdebug"], tmp=tmp_path)
    [(launcher, text)] = p.files
    assert launcher == tmp_path / "data" / "memdebug-watch.ps1"
    lines = text.splitlines()
    assert lines[1] == f"Set-Location -LiteralPath '{tmp_path / 'data'}'"  # starts in the data folder, where nothing can shadow a Python module
    assert lines[2] == "& 'C:\\Program Files\\Py thon\\python.exe' '-m' 'memdebug' 'watch' '--every' '60' '--no-bell' '--log' '/data/watch.log'"
    create, run_now = p.steps
    assert create[0] == TOOLS["schtasks"] and create[1:4] == ("/Create", "/TN", "memdebug-watch") and "/RL" in create and "LIMITED" in create and "ONLOGON" in create
    assert create[-1] == f'"{TOOLS["powershell"]}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{launcher}"'
    assert run_now == (TOOLS["schtasks"], "/Run", "/TN", "memdebug-watch")
    assert all(step[0] == TOOLS["schtasks"] for step in (*p.steps, *p.undo_steps, p.query))
    assert p.undo_remove == (launcher,) and not any("/RU" in step or "SYSTEM" in " ".join(step) for step in p.steps)  # never another account


def test_an_apostrophe_in_a_windows_path_cannot_break_out_of_the_script_quoting(tmp_path):
    p = plan("windows", ["C:\\Users\\O'Brien\\python.exe", "-m", "memdebug"], tmp=tmp_path)
    assert "'C:\\Users\\O''Brien\\python.exe'" in p.files[0][1]


def test_the_linux_plan_is_a_systemd_user_service_with_quoted_arguments(tmp_path):
    p = plan("linux", ["/home/a b/venv/bin/python", "-m", "memdebug"], ["watch", "--log", '/data/we"ird %h/watch.log'], tmp=tmp_path)
    [(unit, text)] = p.files
    assert unit == tmp_path / "home" / ".config" / "systemd" / "user" / "memdebug-watch.service"
    exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert exec_line == 'ExecStart="/home/a b/venv/bin/python" "-m" "memdebug" "watch" "--log" "/data/we\\"ird %%h/watch.log"'
    assert "WantedBy=default.target" in text and "Restart=on-failure" in text
    assert p.steps == ((TOOLS["systemctl"], "--user", "daemon-reload"), (TOOLS["systemctl"], "--user", "enable", "--now", "memdebug-watch.service"))
    assert p.undo_steps == ((TOOLS["systemctl"], "--user", "disable", "--now", "memdebug-watch.service"),)


def test_the_linux_service_goes_where_xdg_config_home_says(tmp_path):
    p = plan("linux", tmp=tmp_path, config_home=tmp_path / "cfg")
    assert p.files[0][0] == tmp_path / "cfg" / "systemd" / "user" / "memdebug-watch.service"


def test_the_macos_plan_is_a_launch_agent_that_round_trips_through_a_plist_parser(tmp_path):
    p = plan("macos", ["/Applications/memdebug & co/memdebug"], ["watch", "--log", "/d/<x>.log"], tmp=tmp_path)
    [(agent, text)] = p.files
    assert agent == tmp_path / "home" / "Library" / "LaunchAgents" / "io.github.juraj-jumic.memdebug.plist"
    data = plistlib.loads(text.encode("utf-8"))
    assert data["ProgramArguments"] == ["/Applications/memdebug & co/memdebug", "watch", "--log", "/d/<x>.log"] and data["RunAtLoad"] is True
    assert p.steps == ((TOOLS["launchctl"], "bootstrap", "gui/501", str(agent)),) and p.undo_steps == ((TOOLS["launchctl"], "bootout", "gui/501", str(agent)),)


def test_an_unknown_system_is_refused():
    with pytest.raises(au.AutostartError):
        au.make_plan("beos", ["x"], [], data_dir=Path0, home=Path0)


def test_the_default_tools_are_absolute_paths_on_every_system():
    for system in ("windows", "linux", "macos"):
        assert all(os.path.isabs(v) or v.startswith(("/", "C:\\")) or v[1:3] == ":\\" for v in au.default_tools(system).values())


def test_the_watcher_command_is_quiet_logs_to_a_file_and_passes_the_ledger_only_when_one_was_named():
    assert au.watch_arguments(30, Path("/d/watch.log"), None) == ["watch", "--every", "30", "--no-bell", "--log", str(Path("/d/watch.log"))]
    assert au.watch_arguments(30, Path("/d/watch.log"), Path("/d/l.db"))[-2:] == ["--db", str(Path("/d/l.db"))]


def test_the_launch_command_is_this_python_or_the_stand_alone_program(monkeypatch):
    assert au.launch_command() == [sys.executable, "-m", "memdebug"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert au.launch_command() == [sys.executable]


# -- applying and removing ---------------------------------------------------------------------------------------------------------

def test_install_writes_the_files_then_runs_the_steps_in_order(tmp_path):
    p = plan("linux", tmp=tmp_path)
    run = Recorder()
    lines = au.install(p, run)
    unit = p.files[0][0]
    assert unit.read_text(encoding="utf-8") == p.files[0][1] and tuple(run.calls) == p.steps
    assert lines[0] == f"wrote: {unit}" and len(lines) == 3


@posix_only
def test_what_it_writes_is_private_to_the_user(tmp_path):
    p = plan("linux", tmp=tmp_path)
    au.install(p, Recorder())
    assert (p.files[0][0].stat().st_mode & 0o777) == 0o600


def test_a_failing_step_undoes_everything_and_says_which_step_failed(tmp_path):
    p = plan("linux", tmp=tmp_path)
    run = Recorder(fail_on="enable")
    with pytest.raises(au.AutostartError, match="nothing was left behind"):
        au.install(p, run)
    assert not p.files[0][0].exists() and p.undo_steps[0] in run.calls


def test_it_will_not_write_over_a_folder_or_leave_a_half_done_install(tmp_path):
    p = plan("linux", tmp=tmp_path)
    target = p.files[0][0]
    target.mkdir(parents=True)  # something that is not a plain file sits where the unit goes
    run = Recorder()
    with pytest.raises(au.AutostartError, match="not a plain file"):
        au.install(p, run)
    assert run.calls == [] and target.is_dir()


@posix_only
def test_it_never_writes_through_a_link(tmp_path):
    p = plan("linux", tmp=tmp_path)
    target = p.files[0][0]
    target.parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    os.symlink(victim, target)
    with pytest.raises(au.AutostartError):
        au.install(p, Recorder())
    assert victim.read_text(encoding="utf-8") == "keep me"


def test_installing_again_replaces_its_own_file(tmp_path):
    p = plan("linux", tmp=tmp_path)
    au.install(p, Recorder())
    au.install(p, Recorder())
    assert p.files[0][0].read_text(encoding="utf-8") == p.files[0][1]


def test_remove_runs_the_undo_steps_and_deletes_only_its_own_file(tmp_path):
    p = plan("linux", tmp=tmp_path)
    au.install(p, Recorder())
    other = p.files[0][0].with_name("other.service")
    other.write_text("not ours", encoding="utf-8")
    run = Recorder()
    lines = au.remove(p, run)
    assert not p.files[0][0].exists() and other.exists() and tuple(run.calls) == p.undo_steps
    assert any(line.startswith("removed:") for line in lines)


def test_remove_reports_a_failing_undo_step_without_stopping(tmp_path):
    p = plan("linux", tmp=tmp_path)
    au.install(p, Recorder())
    lines = au.remove(p, Recorder(fail_on="disable"))
    assert any("nothing to undo" in line for line in lines) and not p.files[0][0].exists()


def test_installed_needs_both_the_query_and_the_file(tmp_path):
    p = plan("linux", tmp=tmp_path)
    assert au.installed(p, Recorder()) is False  # no file yet
    au.install(p, Recorder())
    assert au.installed(p, Recorder()) is True
    assert au.installed(p, Recorder(fail_on="is-enabled")) is False  # the file is there but the service is not enabled
    mac = plan("macos", tmp=tmp_path)
    assert au.installed(mac, Recorder()) is False
    au.install(mac, Recorder())
    assert au.installed(mac, Recorder()) is True  # macOS has no query: the agent file says it


def test_commands_are_run_without_a_shell_and_a_missing_program_is_reported_not_raised(monkeypatch):
    seen = {}

    def fake(args, **kw):
        seen.update(kw, args=args)
        raise FileNotFoundError("no such program")

    monkeypatch.setattr(au.subprocess, "run", fake)
    assert au.default_run(("/nope", "x"))[0] == 127
    assert seen["args"] == ["/nope", "x"] and not seen.get("shell") and seen["timeout"] == au.COMMAND_TIMEOUT


# -- the commands -------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def setup_home(tmp_path, monkeypatch):
    """A made-up home and data folder, one watched store, and no real command ever run."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(au, "system_name", lambda: "linux")
    recorder = Recorder()
    monkeypatch.setattr(au, "default_run", recorder)
    db = tmp_path / "data" / "l.db"
    db.parent.mkdir()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("hello\n", encoding="utf-8")
    assert runner.invoke(cli.app, ["add", str(notes), "--name", "n", "--db", str(db)]).exit_code == 0
    unit = home / ".config" / "systemd" / "user" / "memdebug-watch.service"
    return db, unit, recorder


def test_without_apply_it_only_shows_the_plan_and_changes_nothing(setup_home):
    db, unit, recorder = setup_home
    out = runner.invoke(cli.app, ["autostart", "install", "--db", str(db)])
    assert out.exit_code == 0 and "systemd user service" in out.output and "Nothing has been changed" in out.output
    assert not unit.exists() and recorder.calls == []


def test_apply_without_a_yes_asks_first_and_a_no_changes_nothing(setup_home):
    db, unit, recorder = setup_home
    out = runner.invoke(cli.app, ["autostart", "install", "--apply", "--db", str(db)], input="n\n")
    assert out.exit_code == 1 and "Nothing was changed" in out.output and not unit.exists() and recorder.calls == []


def test_apply_with_yes_sets_it_up_and_status_and_remove_follow(setup_home):
    db, unit, recorder = setup_home
    out = runner.invoke(cli.app, ["autostart", "install", "--apply", "--yes", "--every", "30", "--db", str(db)])
    assert out.exit_code == 0 and "Done" in out.output and unit.exists()
    text = unit.read_text(encoding="utf-8")
    assert '"--every" "30"' in text and "watch.log" in text and au._systemd_quote(str(db.resolve())) in text  # the ledger that was named is the one watched
    assert "is set up" in runner.invoke(cli.app, ["autostart", "status", "--db", str(db)]).output
    dry = runner.invoke(cli.app, ["autostart", "remove", "--db", str(db)])
    assert "Nothing has been changed" in dry.output and unit.exists()
    done = runner.invoke(cli.app, ["autostart", "remove", "--apply", "--db", str(db)])
    assert done.exit_code == 0 and not unit.exists()
    assert "not set up" in runner.invoke(cli.app, ["autostart", "status", "--db", str(db)]).output


def test_with_nothing_watched_it_says_to_run_setup_first(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "default_run", Recorder())
    out = runner.invoke(cli.app, ["autostart", "install", "--apply", "--yes", "--db", str(tmp_path / "l.db")])
    assert out.exit_code == 2 and "memdebug setup" in out.output


def test_removing_what_was_never_set_up_is_a_calm_no_op(setup_home):
    db, unit, recorder = setup_home
    out = runner.invoke(cli.app, ["autostart", "remove", "--apply", "--db", str(db)])
    assert out.exit_code == 0 and "not set up" in out.output and recorder.calls == []


def test_the_error_type_is_a_memdebug_error():
    assert issubclass(au.AutostartError, MemdebugError) and st.KINDS  # reported by the usual error handling, never a traceback


def test_the_service_always_names_the_ledger_even_when_it_is_the_default_one(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "shell-data"  # what only THIS shell's environment says; a service would not see it
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(data))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(au, "system_name", lambda: "linux")
    monkeypatch.setattr(au, "default_run", Recorder())
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("hello\n", encoding="utf-8")
    assert runner.invoke(cli.app, ["add", str(notes), "--name", "n"]).exit_code == 0  # no --db: the default ledger
    assert runner.invoke(cli.app, ["autostart", "install", "--apply", "--yes"]).exit_code == 0
    text = (home / ".config" / "systemd" / "user" / "memdebug-watch.service").read_text(encoding="utf-8")
    default_ledger = (data / "memdebug" / "ledger.db").resolve()
    assert '"--db" ' + au._systemd_quote(str(default_ledger)) in text
