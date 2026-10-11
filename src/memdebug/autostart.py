"""Starting `memdebug watch` at login, without a terminal: a plan for each system, shown first and carried out only on request.

`memdebug watch` stops when its terminal closes or the computer restarts, and a change that is made and undone in between is then never seen.
This module describes how to have the system start it for you: a per-user scheduled task on Windows, a systemd user service on Linux and a launch
agent on macOS. Nothing needs administrator rights, nothing is installed system-wide, and everything it creates is named `memdebug-watch` (or the
macOS label) so that `remove` can undo it exactly.

The plan is pure data, built from fixed words and the paths of the running program: it is printed before anything happens, and applying it
writes only the files listed and runs only the commands listed (never through a shell). Nothing here is read from a store or from memory text.
"""
from __future__ import annotations

import getpass
import os
import plistlib
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .errors import MemdebugError

SERVICE = "memdebug-watch"
LABEL = "io.github.juraj-jumic.memdebug"
COMMAND_TIMEOUT = 30


class AutostartError(MemdebugError):
    """Starting at login could not be set up or removed."""


@dataclass(frozen=True)
class Plan:
    """What installing (and removing) the background watcher involves on one system.

    Attributes:
        system: "windows", "linux" or "macos".
        summary: Plain sentences saying what will be created and run.
        files: Files to create, as (path, text).
        steps: Commands to run after the files are written, each an argument list.
        undo_steps: Commands that undo `steps`.
        undo_remove: Files to delete when removing.
        query: A command whose success means the watcher is set up (None if the files alone say so).
    """

    system: str
    summary: tuple[str, ...]
    files: tuple[tuple[Path, str], ...]
    steps: tuple[tuple[str, ...], ...]
    undo_steps: tuple[tuple[str, ...], ...]
    undo_remove: tuple[Path, ...]
    query: tuple[str, ...] | None = None


Runner = Callable[[tuple[str, ...]], tuple[int, str]]


def system_name() -> str:
    """"windows", "macos" or "linux" for the running system."""
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def launch_command() -> list[str]:
    """How to start memdebug again: the stand-alone program itself, or this Python with `-m memdebug`."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "memdebug"]


def watch_arguments(every: float, log: Path, db: Path | None) -> list[str]:
    """The arguments after the program name: a quiet watcher that logs to a file, on the ledger in use."""
    arguments = ["watch", "--every", f"{every:g}", "--no-bell", "--log", str(log)]
    return arguments + (["--db", str(db)] if db is not None else [])


_POWERSHELL_QUOTES = re.compile("(['\u2018\u2019\u201a\u201b])")  # PowerShell ends a single-quoted string at any of these, not only at '


def _powershell_quote(text: str) -> str:
    """One word for a PowerShell script, single-quoted, with every kind of single quote doubled (the only way to write one inside the quotes)."""
    return "'" + _POWERSHELL_QUOTES.sub(r"\1\1", text) + "'"


def _systemd_quote(text: str) -> str:
    """One word of a systemd ExecStart line: in double quotes, with backslash, quote, percent and dollar escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _systemd_path(text: str) -> str:
    """A path for a systemd setting that is not a command line: only the percent sign needs escaping."""
    return text.replace("%", "%%")


def _refuse_control_characters(*words: object) -> None:
    """A newline or other control character in a path or argument could add lines to a service file or script: refuse them all."""
    for word in words:
        text = str(word)
        if any(ord(ch) < 32 or ch in "\x7f\x85\u2028\u2029" for ch in text):
            raise AutostartError("a path or argument contains a control character, so nothing was set up")


def default_tools(system: str) -> dict[str, str]:
    """The system programs a plan runs, by absolute path (never found through PATH, which another program could have changed)."""
    if system == "windows":
        system32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
        return {"schtasks": str(system32 / "schtasks.exe"), "powershell": str(system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")}
    return {"systemctl": "/usr/bin/systemctl", "launchctl": "/bin/launchctl"}


def current_user() -> str:
    """The Windows account the task is for: the domain (the computer's name for a local account), a backslash, and the user name."""
    name = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{name}" if domain else name


def _task_xml(user: str, program: str, arguments: str) -> str:
    """The scheduled task as XML.

    A logon trigger for a named user, run as that user with ordinary rights, can be created without administrator rights; `schtasks /Create /SC ONLOGON`
    cannot (it answers "Access is denied", found when 1.0.0 was first tried on a real PC).
    """
    who, exe, args = escape(user), escape(program), escape(arguments)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        "  <RegistrationInfo><Description>memdebug watch: record changes to an AI agent's memory. Undo with 'memdebug autostart remove'.</Description></RegistrationInfo>\r\n"
        f"  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{who}</UserId></LogonTrigger></Triggers>\r\n"
        f'  <Principals><Principal id="Author"><UserId>{who}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\r\n'
        "  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>"
        "<AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
        "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>\r\n"
        f'  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>{args}</Arguments></Exec></Actions>\r\n'
        "</Task>\r\n")


def _stop_leftover(powershell: str, words: list[str]) -> tuple[tuple[str, ...], ...]:
    """The step that stops a watcher the task left running.

    Ending a Windows task stops only its launcher, not the watcher it started (found when `remove` was first tried on a real PC: two watchers kept
    running on a ledger whose task was gone). The step stops only a process whose command line holds this watcher's own log file after `--log`,
    the word memdebug and the word watch, and never itself. Nothing is stopped when the plan has no log file to recognise it by.
    """
    if "--log" not in words or words.index("--log") + 1 >= len(words):
        return ()
    log = words[words.index("--log") + 1]
    script = (f"$log = {_powershell_quote(log)}; Get-CimInstance Win32_Process | Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine "
              "-and $_.CommandLine.Contains('--log') -and $_.CommandLine.Contains($log) -and $_.CommandLine.Contains('memdebug') "
              "-and $_.CommandLine.Contains(' watch ') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    return ((powershell, "-NoProfile", "-NonInteractive", "-Command", script),)


def make_plan(system: str, command: list[str], arguments: list[str], *, data_dir: Path, home: Path, uid: int = 0,
              config_home: Path | None = None, tools: dict[str, str] | None = None, launcher_dir: Path | None = None,
              user: str | None = None) -> Plan:
    """The plan for a system.

    Args:
        system: "windows", "linux" or "macos".
        command: How to start memdebug (see `launch_command`).
        arguments: What to give it (see `watch_arguments`).
        data_dir: The memdebug data folder (where the ledger is). The watcher starts in it, so nothing in the home folder can shadow a Python module.
        home: The person's home folder.
        uid: The numeric user id (macOS launchctl needs it).
        config_home: Linux only: the configuration folder (`$XDG_CONFIG_HOME`, default `~/.config`).
        tools: The absolute paths of the system programs to run (see `default_tools`).
        user: Windows only: the account the task is for (domain, backslash, name); the current account when omitted.
        launcher_dir: Windows only: where the launcher script is kept. It runs at every login, so it belongs in a folder only the person can write
            (the default memdebug data folder under their profile), whatever folder the ledger is in; `data_dir` when omitted.

    Raises:
        AutostartError: If the system is not one of the three, or a path or argument holds a control character.
    """
    full = [*command, *arguments]
    _refuse_control_characters(*full, data_dir, home, config_home or "", launcher_dir or "")
    tool = tools or default_tools(system)
    if system == "windows":
        launcher = (launcher_dir or data_dir) / f"{SERVICE}.ps1"
        text = ("# Started at login by 'memdebug autostart install'. Undo with 'memdebug autostart remove'.\r\n"
                f"Set-Location -LiteralPath {_powershell_quote(str(data_dir))}\r\n"
                "& " + " ".join(_powershell_quote(part) for part in full) + "\r\n")
        who = user or current_user()
        _refuse_control_characters(who)
        task_file = launcher.with_suffix(".xml")
        arguments_line = f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{launcher}"'
        return Plan(
            "windows",
            (f"Create a small launcher script: {launcher}",
             f"Create a Windows scheduled task named {SERVICE} from {task_file.name} that runs it when {who} logs in (your account only, no administrator rights).",
             "Start it now. It runs in a hidden window; what it finds goes to the log file and the viewer's Status page.",),
            ((launcher, text), (task_file, _task_xml(who, tool["powershell"], arguments_line))),
            ((tool["schtasks"], "/Create", "/TN", SERVICE, "/XML", str(task_file), "/F"), (tool["schtasks"], "/Run", "/TN", SERVICE)),
            ((tool["schtasks"], "/End", "/TN", SERVICE), (tool["schtasks"], "/Delete", "/TN", SERVICE, "/F"), *_stop_leftover(tool["powershell"], full)),
            (launcher, task_file),
            (tool["schtasks"], "/Query", "/TN", SERVICE))
    if system == "linux":
        unit = (config_home or home / ".config") / "systemd" / "user" / f"{SERVICE}.service"
        text = ("[Unit]\nDescription=memdebug watch: record changes to an AI agent's memory\n\n"
                "[Service]\nWorkingDirectory=" + _systemd_path(str(data_dir)) + "\nExecStart=" + " ".join(_systemd_quote(part) for part in full)
                + "\nRestart=on-failure\nRestartSec=30\n\n"
                "[Install]\nWantedBy=default.target\n")
        return Plan(
            "linux",
            (f"Create a systemd user service: {unit}",
             f"Enable and start it with 'systemctl --user enable --now {SERVICE}.service' (your account only; it starts at login).",
             "What it finds goes to the log file and the viewer's Status page."),
            ((unit, text),),
            ((tool["systemctl"], "--user", "daemon-reload"), (tool["systemctl"], "--user", "enable", "--now", f"{SERVICE}.service")),
            ((tool["systemctl"], "--user", "disable", "--now", f"{SERVICE}.service"),),
            (unit,),
            (tool["systemctl"], "--user", "is-enabled", f"{SERVICE}.service"))
    if system == "macos":
        agent = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        text = plistlib.dumps({"Label": LABEL, "ProgramArguments": full, "WorkingDirectory": str(data_dir), "RunAtLoad": True, "KeepAlive": True,
                               "ThrottleInterval": 30, "ProcessType": "Background"}).decode("utf-8")
        return Plan(
            "macos",
            (f"Create a launch agent: {agent}",
             f"Load it with 'launchctl bootstrap gui/{uid}' (your account only; it starts at login).",
             "What it finds goes to the log file and the viewer's Status page."),
            ((agent, text),),
            ((tool["launchctl"], "bootstrap", f"gui/{uid}", str(agent)),),
            ((tool["launchctl"], "bootout", f"gui/{uid}", str(agent)),),
            (agent,))
    raise AutostartError("starting at login is set up for Windows, Linux and macOS only")


def default_run(args: tuple[str, ...]) -> tuple[int, str]:
    """Run one command without a shell, with a time limit; returns its exit code and a short piece of its output."""
    try:
        done = subprocess.run(list(args), capture_output=True, text=True, timeout=COMMAND_TIMEOUT, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, type(exc).__name__
    return done.returncode, (done.stdout + done.stderr).strip()[:300]


def _plain_file(path: Path) -> bool:
    """Whether the path is free to write: it does not exist, or is a plain file (never a link or a folder)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _plain_file(path):
        raise AutostartError(f"{path.name} exists and is not a plain file, so it was not touched")
    # Windows PowerShell 5.1 reads a script without a byte order mark in the ANSI code page, which turns a name such as "José" into something else;
    # a task definition is UTF-16, the form Task Scheduler itself writes and the one its XML declaration here names
    encoding = {".ps1": "utf-8-sig", ".xml": "utf-16"}.get(path.suffix.lower(), "utf-8")
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise AutostartError(f"{path.name} holds a character that cannot be written, so nothing was set up") from exc
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _unlink(path: Path) -> None:
    try:
        if _plain_file(path):
            path.unlink()
    except OSError:
        pass


def install(plan: Plan, run: Runner | None = None) -> list[str]:
    """Carry out a plan. If a command fails, what was created is removed again and an error says which step failed.

    Raises:
        AutostartError: If a file cannot be written safely or a command fails.
    """
    run = run or default_run
    written: list[Path] = []
    try:
        for path, text in plan.files:
            _write(path, text)
            written.append(path)
    except OSError as exc:
        for path in written:
            _unlink(path)
        raise AutostartError(f"could not write the files ({exc.strerror})") from exc
    except AutostartError:
        for path in written:
            _unlink(path)
        raise
    done: list[str] = []
    for step in plan.steps:
        code, output = run(step)
        if code != 0:
            for undo in plan.undo_steps:
                run(undo)
            for path in written:
                _unlink(path)
            raise AutostartError(f"'{' '.join(step[:3])}' failed ({output or 'exit ' + str(code)}); nothing was left behind")
        done.append(f"ran: {' '.join(step[:4])}{' ...' if len(step) > 4 else ''}")
    return [f"wrote: {path}" for path in written] + done


def remove(plan: Plan, run: Runner | None = None) -> list[str]:
    """Undo what `install` did. Commands that fail (because it was not set up) are reported, not fatal."""
    run = run or default_run
    lines = []
    for step in plan.undo_steps:
        code, _ = run(step)
        lines.append(f"ran: {' '.join(step[:4])}" + ("" if code == 0 else " (nothing to undo)"))
    for path in plan.undo_remove:
        if os.path.lexists(path):
            _unlink(path)
            lines.append(f"removed: {path}")
    return lines


def installed(plan: Plan, run: Runner | None = None) -> bool:
    """Whether the watcher looks set up: its query succeeds, or (macOS) its agent file is there."""
    run = run or default_run
    if not all(os.path.lexists(p) for p in plan.undo_remove):
        return False  # nothing was written, so there is nothing to ask the system about
    return plan.query is None or run(plan.query)[0] == 0
