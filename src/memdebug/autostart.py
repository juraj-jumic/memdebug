"""Starting `memdebug watch` at login, without a terminal: a plan for each system, shown first and carried out only on request.

`memdebug watch` stops when its terminal closes or the computer restarts, and a change that is made and undone in between is then never seen.
This module describes how to have the system start it for you: a per-user scheduled task on Windows, a systemd user service on Linux and a launch
agent on macOS. Nothing needs administrator rights, nothing is installed system-wide, and everything it creates is named `memdebug-watch` (or the
macOS label) so that `remove` can undo it exactly.

The plan is pure data, built from fixed words and the paths of the running program: it is printed before anything happens, and applying it
writes only the files listed and runs only the commands listed (never through a shell). Nothing here is read from a store or from memory text.
"""
from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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


def _powershell_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _systemd_quote(text: str) -> str:
    """One word of a systemd ExecStart line: in double quotes, with backslash, quote and percent escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def default_tools(system: str) -> dict[str, str]:
    """The system programs a plan runs, by absolute path (never found through PATH, which another program could have changed)."""
    if system == "windows":
        system32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
        return {"schtasks": str(system32 / "schtasks.exe"), "powershell": str(system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")}
    return {"systemctl": "/usr/bin/systemctl", "launchctl": "/bin/launchctl"}


def make_plan(system: str, command: list[str], arguments: list[str], *, data_dir: Path, home: Path, uid: int = 0,
              config_home: Path | None = None, tools: dict[str, str] | None = None) -> Plan:
    """The plan for a system.

    Args:
        system: "windows", "linux" or "macos".
        command: How to start memdebug (see `launch_command`).
        arguments: What to give it (see `watch_arguments`).
        data_dir: The memdebug data folder, where a Windows launcher file is kept.
        home: The person's home folder.
        uid: The numeric user id (macOS launchctl needs it).
        config_home: Linux only: the configuration folder (`$XDG_CONFIG_HOME`, default `~/.config`).
        tools: The absolute paths of the system programs to run (see `default_tools`).

    Raises:
        AutostartError: If the system is not one of the three.
    """
    full = [*command, *arguments]
    tool = tools or default_tools(system)
    if system == "windows":
        launcher = data_dir / f"{SERVICE}.ps1"
        text = ("# Started at login by 'memdebug autostart install'. Undo with 'memdebug autostart remove'.\r\n"
                "& " + " ".join(_powershell_quote(part) for part in full) + "\r\n")
        run_line = f'"{tool["powershell"]}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{launcher}"'
        return Plan(
            "windows",
            (f"Create a small launcher script: {launcher}",
             f"Create a Windows scheduled task named {SERVICE} that runs it when you log in (your account only, no administrator rights).",
             "Start it now. It runs in a hidden window; what it finds goes to the log file and the viewer's Status page.",),
            ((launcher, text),),
            ((tool["schtasks"], "/Create", "/TN", SERVICE, "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR", run_line), (tool["schtasks"], "/Run", "/TN", SERVICE)),
            ((tool["schtasks"], "/End", "/TN", SERVICE), (tool["schtasks"], "/Delete", "/TN", SERVICE, "/F")),
            (launcher,),
            (tool["schtasks"], "/Query", "/TN", SERVICE))
    if system == "linux":
        unit = (config_home or home / ".config") / "systemd" / "user" / f"{SERVICE}.service"
        text = ("[Unit]\nDescription=memdebug watch: record changes to an AI agent's memory\n\n"
                "[Service]\nExecStart=" + " ".join(_systemd_quote(part) for part in full) + "\nRestart=on-failure\nRestartSec=30\n\n"
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
        text = plistlib.dumps({"Label": LABEL, "ProgramArguments": full, "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                               "ProcessType": "Background"}).decode("utf-8")
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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


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
