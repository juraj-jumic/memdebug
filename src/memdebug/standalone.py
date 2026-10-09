"""What the stand-alone Windows program does when someone opens it by double-clicking instead of from a terminal.

A double-click starts a command-line program with no arguments in a console window of its own, which closes the moment the program ends. memdebug would
print its help and exit, and the window would vanish before anyone could read it. Instead it says what to do and waits for Enter.

How it tells: only in a stand-alone build on Windows, with no arguments, with the input and output both attached to a console, and with nothing else on
that console. A one-file build is two processes (a launcher and the program), so a console of its own holds exactly two; a terminal that was already
open holds at least one more (the shell). This was measured on Windows 11, with a throwaway program, in a new window (2) and from a shell (4).
"""
from __future__ import annotations

import os
import sys
from typing import Callable

MESSAGE = """memdebug is a command-line program, so there is nothing to show when it is opened by double-clicking.

Open PowerShell (or any terminal) in the folder this file is in and type one of:

    .\\{name} demo        a short tour with made-up data; nothing of yours is touched
    .\\{name} --help      every command
    .\\{name} setup       find your AI agent's memory and start watching it

More: https://github.com/juraj-jumic/memdebug#readme
"""


def console_process_count() -> int | None:
    """How many processes are attached to this process's console, or None where that cannot be asked (not Windows, or the call fails)."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = (ctypes.c_uint * 64)()
        return int(ctypes.windll.kernel32.GetConsoleProcessList(buffer, 64))  # type: ignore[attr-defined, unused-ignore]  # windll exists only on Windows
    except (OSError, AttributeError, ValueError):
        return None


def started_by_double_click(argv: list[str], *, frozen: bool, windows: bool, process_count: int | None, interactive: bool) -> bool:
    """Whether this looks like a double-click: a stand-alone Windows build, no arguments, a console of its own and nobody typing a command.

    Args:
        argv: The command line, program name first.
        frozen: True in a stand-alone build.
        windows: True on Windows.
        process_count: What `console_process_count` returned.
        interactive: True if both input and output are a console (not redirected).
    """
    return frozen and windows and len(argv) == 1 and interactive and process_count is not None and 1 <= process_count <= 2


def explain_if_double_clicked(argv: list[str] | None = None, *, wait: Callable[[str], object] = input) -> bool:
    """Show the message and wait for Enter if the program was opened by double-click.

    Args:
        argv: The command line (the real one when omitted).
        wait: Called with a prompt to wait for Enter (replaced in tests).

    Returns:
        True if the message was shown, in which case the caller should exit without running anything.
    """
    argv = sys.argv if argv is None else argv
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)() and getattr(sys.stdout, "isatty", lambda: False)())
    if not started_by_double_click(argv, frozen=bool(getattr(sys, "frozen", False)), windows=os.name == "nt",
                                   process_count=console_process_count(), interactive=interactive):
        return False
    print(MESSAGE.format(name=os.path.basename(sys.executable)))
    try:
        wait("Press Enter to close this window. ")
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    return True
