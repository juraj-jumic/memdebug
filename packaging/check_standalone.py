"""Smoke test of a built stand-alone program: `python packaging/check_standalone.py PATH_TO_PROGRAM`.

Run by CI and by the release workflow on every operating system before a build is used. It starts the program the way a person would, in an empty
folder, and fails unless:

* `--version` prints the version of the package installed in the Python that runs this script,
* `demo` finishes with exit code 0, and
* `selftest` exits 0 and every protection it can prove here is PASS, not SKIP. A stand-alone build once SKIPped the tripwire checks and passed the
  stalled-process check for nothing, so these names are required to PASS, and
* on Windows, started with no arguments on a console of its own (what a double-click gives), it stays open and waits instead of printing its help
  and closing at once.
"""
from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
from pathlib import Path

DOUBLE_CLICK_WAIT = 5.0  # seconds a program started like a double-click must still be open after

REQUIRED = (
    "git ignores global config",
    "hostile repository config cannot run programs",
    "a hung process tree is killed",
    "symlinks are not followed",
    "dangerous file names are rejected",
    "ledger tamper detection",
    "viewer is local-only and protected",
    "rollback is safe",
    "folder rollback is safe",
)


def run(program: str, *args: str, folder: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run the program with these arguments in `folder` and return what it printed."""
    return subprocess.run([program, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=folder, timeout=timeout)


def double_click_problem(program: str, folder: str, wait: float) -> str | None:
    """Windows only: start the program with no arguments on a console of its own, as a double-click does (but with no window shown).

    Returns:
        What is wrong, or None if the program stayed open (it is waiting for Enter, which is what a person needs in order to read its message).
    """
    proc = subprocess.Popen([program], cwd=folder, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # a console of its own, with no window to flash up
    try:
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            return None
        return f"opened on a console of its own it exited at once (code {proc.returncode}), so a double-click would flash and vanish"
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)  # the program and the process it started


def main(program: str) -> int:
    """Check the program at this path; print what was found and return 0 if it is sound, else 1."""
    program = str(Path(program).resolve())
    problems: list[str] = []
    expected = subprocess.run([sys.executable, "-m", "memdebug", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"{program} on {platform.system()} {platform.machine()}; expecting '{expected}'")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        shown = run(program, "--version", folder=folder)
        print(f"--version: {shown.stdout.strip()!r} (exit {shown.returncode})")
        if shown.returncode != 0 or shown.stdout.strip() != expected:
            problems.append(f"--version printed {shown.stdout.strip()!r}, expected {expected!r}")

        demo = run(program, "demo", folder=folder)
        print(f"demo: exit {demo.returncode}")
        if demo.returncode != 0:
            problems.append(f"demo exited {demo.returncode}: {(demo.stdout + demo.stderr)[-400:]}")

        selftest = run(program, "selftest", folder=folder)
        print(selftest.stdout)
        if selftest.returncode != 0:
            problems.append(f"selftest exited {selftest.returncode}")
        results: dict[str, str] = {}
        for line in selftest.stdout.splitlines():
            if line.startswith("[") and "]" in line and ":" in line:
                results[line.split(":", 1)[0].split("]", 1)[1].strip()] = line[1:line.index("]")]
        for name in REQUIRED:
            if results.get(name) != "PASS":
                problems.append(f"selftest check '{name}' is {results.get(name, 'missing')}, not PASS")
        if platform.system() == "Windows":
            problem = double_click_problem(program, folder, DOUBLE_CLICK_WAIT)
            print(f"double-click: {'stays open, as it should' if problem is None else problem}")
            if problem is not None:
                problems.append(problem)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    print("stand-alone build OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python packaging/check_standalone.py PATH_TO_PROGRAM")
    sys.exit(main(sys.argv[1]))
