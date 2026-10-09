"""`memdebug selftest`: prove on THIS machine that the protections work.

Several protections depend on the operating system and on the installed git, so a green test suite
on another platform proves little. Each check here either demonstrates a protection, or says SKIP
and why. A SKIP is not a pass.

Where a check guards against an attack, it first runs a CONTROL that shows the attack really works
on this machine without the protection. If the control cannot be made to work, the check is a SKIP,
because "nothing bad happened" would prove nothing.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .adapters.common import Warnings
from .adapters.markdown_git import (
    MarkdownGitAdapter,
    _clean_env,
    _Run,
    _spawn_flags,
    _valid_relpath,
    find_git,
)
from .adapters.mem0 import Mem0Adapter
from .ledger import Ledger
from .models import Memory, MemoryEvent, Op
from .paths import default_ledger_path
from .textsafe import console_safe, safe_text

Result = tuple[str, str, str]  # name, PASS | FAIL | SKIP | INFO, detail


class _Skip(Exception):
    pass


def _tmp() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _git_run(git: str, cwd: str | Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git, "-c", "user.name=selftest", "-c", "user.email=selftest@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), env=env or _clean_env(), capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
    )


def _git_version(git: str) -> tuple[int, int] | None:
    out = subprocess.run([git, "--version"], capture_output=True, timeout=30, env=_clean_env()).stdout
    match = re.search(rb"git version (\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _need_git() -> str:
    git = find_git()
    if not git:
        raise _Skip("git not found; the markdown backend is unavailable here")
    version = _git_version(git)
    if not version or version < (2, 31):
        raise _Skip("git older than 2.31; the markdown backend is unavailable here")
    return git


# -- checks ----------------------------------------------------------------------------------


def check_info() -> str:
    """Describe this machine (platform, Python and git versions) for the report's environment line. Proves nothing."""
    git = find_git()
    version = _git_version(git) if git else None
    return (f"{sys.platform}, python {sys.version.split()[0]}, "
            f"git {'.'.join(map(str, version)) if version else 'not found'}"
            + (f" at {git}" if git else ""))


def check_git_ignores_global_config() -> str:
    """Prove that git run by memdebug does not read a global config file.

    Control: git without the isolation setting reads a planted ~/.gitconfig (SKIP if it does not). Then git with
    the isolated environment is asked for the same key and must not see it.
    """
    git = _need_git()
    with _tmp() as home:
        Path(home, ".gitconfig").write_text("[memdebugtest]\n\tkey = leaked\n", encoding="utf-8")
        leak_env = {**_clean_env(), "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home}
        control_env = {k: v for k, v in leak_env.items() if k != "GIT_CONFIG_GLOBAL"}
        control = _git_run(git, home, "config", "--get", "memdebugtest.key", env=control_env)
        if b"leaked" not in control.stdout:
            raise _Skip("could not show that a global config is normally read here, so isolation is unproven")
        ours = _git_run(git, home, "config", "--get", "memdebugtest.key", env=leak_env)
        if b"leaked" in ours.stdout:
            raise AssertionError("git still read a global config although it was switched off")
    return "a global git config pointed at by HOME is not read"


def _config_trap_repo(git: str, root: Path, trap: Path, prefix: str, markers_dir: Path | None = None) -> _Hostile:
    """Build a repository whose config names the tripwire for every setting that makes git run a program.

    Those settings are the ones git uses when it shows a diff, pages output, checks the file system or opens an
    editor. There is one marker, `<prefix>-config`. (The tripwire and its helpers are defined further down, with
    the rollback check.)

    Args:
        git: Path of the git executable.
        root: A new folder to create; the repository goes in `root/repo`.
        trap: The tripwire script that every configured program points at.
        prefix: Names the marker file, so repositories made with different prefixes never share one.
        markers_dir: Where the marker goes; `root` when omitted.

    Returns:
        The repository and its marker.
    """
    root.mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    marker = (markers_dir or root) / f"{prefix}-config"
    command = f'"{Path(sys.executable).as_posix()}" "{trap.as_posix()}" "{marker.as_posix()}" config'
    _git_run(git, repo, "init", "-q", "-b", "main")
    for version in ("one", "two"):
        (repo / "a.md").write_text(version + "\n", encoding="utf-8")
        _git_run(git, repo, "add", "-A")
        _git_run(git, repo, "commit", "-q", "-m", version)
    for key in ("diff.external", "core.fsmonitor", "core.pager", "pager.log", "core.editor"):
        _git_run(git, repo, "config", key, command)
    _git_run(git, repo, "config", "core.hooksPath", str(root))
    return _Hostile(repo, {"config": marker})


def _run_config_control(git: str, hostile: _Hostile) -> bool:
    """CONTROL: ordinary git, asked for a patch with the external diff allowed, runs the program on this machine. True if it did."""
    subprocess.run([git, "log", "-p", "--ext-diff", "--no-color"], cwd=str(hostile.repo), env=_clean_env(),
                   capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    return hostile.markers["config"].exists()


def check_hostile_repo_config_cannot_run_programs() -> str:
    """Prove that reading a repository whose config names programs runs none of them.

    Control: ordinary git, asked for a patch with external diff allowed, runs the tripwire in a first repository
    (SKIP if it does not). Then memdebug's adapter reads history and notes of a second, identical repository
    and its own marker must stay absent.
    """
    git = _need_git()
    with _tmp() as base:
        base_path = Path(base)
        trap = _write_tripwire(base_path / "trap.py")
        # The control and the protected run each get a repository and a marker of their own (see check_rollback_is_safe for why).
        control = _config_trap_repo(git, base_path / "control", trap, "CONTROL")
        protected = _config_trap_repo(git, base_path / "protected", trap, "PWNED")
        if not _run_config_control(git, control):
            raise _Skip("could not make a hostile config run a program here, so the protection is unproven")

        adapter = MarkdownGitAdapter(protected.repo, store="selftest")
        adapter.read_history(100)
        adapter.history("a.md")
        adapter.list_memories({"store": "selftest"})
        if protected.markers["config"].exists():
            raise AssertionError("a program named in the repository's config was executed. What started it: "
                                 + _tripwire_report(protected.markers, ["config"]) + f". {_git_version_line(git)}. {_NOT_A_LEFTOVER}")
    return "a repository config that runs programs did not run any"


def check_history_file_is_read_only() -> str:
    """Prove that the Mem0 history database is opened read-only and a missing one is not created.

    There is no control. The adapter's connection is made to try a write even after `query_only` is switched
    off, and must refuse; the file's hash must be unchanged; opening an adapter on a missing path must not
    create the file.
    """
    class _Stub:
        def get_all(self, **kw):
            return {"results": []}

        def history(self, memory_id):
            return []

    with _tmp() as base:
        path = Path(base) / "history.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, "
                     "new_memory TEXT, event TEXT, created_at DATETIME, updated_at DATETIME)")
        conn.execute("INSERT INTO history VALUES ('1','m','', 'x','ADD','2026-10-04T00:00:00+00:00',NULL)")
        conn.commit()
        conn.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        adapter = Mem0Adapter(_Stub(), path)
        adapter.read_history(10)
        ro = adapter._connect_ro()
        try:
            ro.execute("PRAGMA query_only = OFF")
            try:
                ro.execute("INSERT INTO history (id, memory_id) VALUES ('x', 'y')")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("the history connection accepted a write")
        finally:
            ro.close()
        if hashlib.sha256(path.read_bytes()).hexdigest() != before:
            raise AssertionError("the history file changed")
        missing = Path(base) / "nope.db"
        try:
            Mem0Adapter(_Stub(), missing)
        except Exception:
            pass
        if missing.exists():
            raise AssertionError("a missing history file was created")
    return "history is opened read-only; the file is never changed or created"


def check_hung_process_tree_is_killed() -> str:
    """Prove that a stalled process and the child it started are both stopped after the timeout.

    There is no control. A process starts a sleeping child and itself sleeps for 40 seconds, with a 1 second
    timeout. Reading its output only returns once every holder of the pipe is gone, and that must happen within 15 seconds.
    """
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); time.sleep(40)")
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=errfile, **_spawn_flags())
    run = _Run(proc, errfile, timeout=1.0)
    started = time.monotonic()
    try:
        if proc.stdout is None:
            raise AssertionError("the test process has no output pipe")
        proc.stdout.read()  # returns only when every holder of the pipe, children included, is gone
        elapsed = time.monotonic() - started
    finally:
        run.close()
    if elapsed > 15:
        raise AssertionError(f"a stalled process and its child were only stopped after {elapsed:.0f}s")
    return f"a stalled process and its child were stopped after {elapsed:.1f}s"


def check_symlinks_are_not_followed() -> str:
    """Prove that a note which is a symlink to a secret file is not read.

    The link is made first (SKIP where symlinks cannot be created), then the adapter's file reader is pointed
    at it and must return nothing and leak none of the secret into its warnings.
    """
    with _tmp() as base:
        secret = Path(base) / "secret.txt"
        secret.write_text("TOP SECRET TOKEN", encoding="utf-8")
        link = Path(base) / "link.md"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError, AttributeError):
            raise _Skip("cannot create symlinks here (Windows needs Developer Mode or administrator rights)") from None
        warnings = Warnings()
        text = MarkdownGitAdapter._read_working_file(str(link), "link.md", warnings)
        if text is not None or "TOP SECRET" in repr(warnings.as_list()):
            raise AssertionError("a symlink was followed")
    return "a symlink pointing at a secret file was not read"


def check_junctions_are_not_followed() -> str:
    """Prove that a Windows directory junction pointing outside a repository is not followed.

    SKIP off Windows or where the junction cannot be made. The adapter lists the repository; it must not return
    the secret file behind the junction and must mark the listing incomplete.
    """
    if os.name != "nt":
        raise _Skip("Windows only")
    git = _need_git()
    with _tmp() as base:
        outside = Path(base) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("TOP SECRET TOKEN", encoding="utf-8")
        repo = Path(base) / "repo"
        repo.mkdir()
        _git_run(git, repo, "init", "-q", "-b", "main")
        (repo / "ok.md").write_text("fine\n", encoding="utf-8")
        cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
        made = subprocess.run([cmd, "/c", "mklink", "/J", str(repo / "junc"), str(outside)],
                              capture_output=True, timeout=30)
        if made.returncode != 0:
            raise _Skip("could not create a directory junction here")
        live = MarkdownGitAdapter(repo, store="selftest").list_memories({"store": "selftest"})
        if any("secret" in m.id or "TOP SECRET" in m.text for m in live.memories) or live.complete:
            raise AssertionError("a directory junction was followed")
    return "a directory junction pointing outside the repository was not followed"


def check_dangerous_names_are_rejected() -> str:
    """Prove that the note-name validator rejects reserved and unsafe paths and accepts ordinary ones.

    Runs a fixed list of hostile names (device names, `..`, drive letters, trailing dots, 8.3 aliases and the
    like) and a short list of good ones through the validator. No control is needed: it is a pure function.
    """
    bad = ["NUL.md", "con.md", "aux/x.md", "notes:hidden.md", "C:evil.md", "a.md.", "dir /x.md", ".GIT/x.md", "GIT~1/x.md",
           "a<b>.md", 'q"uote.md', "../x.md", "/abs.md", "COM1.md", "lpt9.txt.md", "a\\b.md", "x\ny.md"]
    good = ["ok/fine.md", "caf\u00e9.md", "People/Ann.md", "-rf.md"]
    wrongly_ok = [p for p in bad if _valid_relpath(p, (".md",)) is not None]
    wrongly_bad = [p for p in good if _valid_relpath(p, (".md",)) is None]
    if wrongly_ok or wrongly_bad:
        raise AssertionError(f"accepted {wrongly_ok!r}; rejected {wrongly_bad!r}")
    return f"{len(bad)} dangerous names rejected, {len(good)} normal names accepted"


def check_console_survives_unencodable_text() -> str:
    """Prove that text the console encoding cannot show is escaped, not left to raise an error.

    Escapes mixed Japanese, emoji and accented text for three legacy encodings and checks the result encodes.
    """
    for encoding in ("cp1252", "ascii", "cp437"):
        shown = console_safe("\u65e5\u672c\u8a9e \U0001f600 caf\u00e9", encoding)
        shown.encode(encoding)  # must be representable
        if "\u65e5" in shown:
            raise AssertionError("characters outside the console encoding were not escaped")
    return "text a console cannot show is escaped instead of crashing"


def check_ledger_tamper_detection() -> str:
    """Prove that the ledger notices an edited entry and an edited snapshot.

    A fresh ledger must verify (the control). Then one event's payload is edited behind the ledger's back and
    verification must fail; after that is undone, a snapshot's stored text is forged and both verification and
    loading that snapshot must fail.
    """
    from .models import Memory

    with _tmp() as base:
        path = Path(base) / "ledger.db"
        ledger = Ledger(path)
        now = datetime.now(timezone.utc)
        ledger.append(MemoryEvent(backend="t", memory_id="m", op=Op.ADD, ts=now, after="x"))
        info = ledger.save_snapshot("t", {"user_id": "u"}, [Memory(id="m", text="the original")],
                                    complete=True, taken_at=now)
        if not ledger.verify().ok:
            raise AssertionError("a freshly written ledger did not verify")
        conn = sqlite3.connect(path)
        conn.execute("UPDATE events SET payload = replace(payload, '\"x\"', '\"y\"') WHERE seq = 1")
        conn.commit()
        if ledger.verify().ok:
            raise AssertionError("an edited ledger entry was not detected")
        conn.execute("UPDATE events SET payload = replace(payload, '\"y\"', '\"x\"') WHERE seq = 1")
        conn.execute("UPDATE blobs SET text = 'forged'")
        conn.commit()
        conn.close()
        if ledger.verify().ok:
            raise AssertionError("an edited snapshot text was not detected")
        try:
            ledger.load_snapshot(info.id)
        except Exception:
            pass
        else:
            raise AssertionError("an edited snapshot was loaded without complaint")
        ledger.close()
    return "an edited ledger entry and an edited snapshot are both detected"


def check_ledger_file_permissions() -> str:
    """Prove that a newly created ledger file is readable and writable by its owner only (mode 600).

    SKIP where the OS is not POSIX, since this tool does not set Windows permissions.
    """
    if os.name != "posix":
        raise _Skip(f"Windows permissions are not set by this tool; the default ledger lives in "
                    f"{default_ledger_path().parent}, which is private to your user account")
    with _tmp() as base:
        path = Path(base) / "ledger.db"
        Ledger(path).close()
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise AssertionError(f"new ledger file has mode {mode:o}, expected 600")
    return "a new ledger file is owner-only (600)"


def check_viewer_is_local_and_protected() -> str:
    """Prove that the viewer serves this computer only and answers only requests that carry its secret.

    Starts a real viewer on a free port and checks that it listens on 127.0.0.1, refuses a request without
    the secret (403), with another host name (421) or with POST (405), and serves the real request (200). It then
    checks that no other program can bind the same port and that a second viewer cannot. On Windows a control
    first shows that ordinary sockets can share a port there, so the exclusive setting matters.
    """
    import http.client
    import socket
    import threading

    from .viewer.server import ViewerServer

    with _tmp() as base:
        path = Path(base) / "ledger.db"
        ledger = Ledger(path)
        ledger.append(MemoryEvent(backend="t", memory_id="m", op=Op.ADD, ts=datetime.now(timezone.utc), after="x"))
        ledger.close()
        server = ViewerServer(path, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        notes = []
        try:
            listening = str(server.server_address[0])
            if listening != "127.0.0.1":
                raise AssertionError(f"the viewer is listening on {listening}, not only on this computer")

            def ask(method="GET", host=None, cookie=True, target="/"):
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
                conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", host or f"127.0.0.1:{server.port}")
                if cookie:
                    conn.putheader("Cookie", f"{server.state.cookie_name}={server.token}")
                conn.endheaders()
                status = conn.getresponse().status
                conn.close()
                return status

            checks = [("no secret", ask(cookie=False), 403), ("another host name", ask(host="evil.example"), 421),
                      ("POST", ask("POST"), 405), ("the real request", ask(), 200)]
            for name, got, want in checks:
                if got != want:
                    raise AssertionError(f"{name}: expected {want}, got {got}")

            def second_bind_succeeds() -> bool:
                other = socket.socket()
                try:
                    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    other.bind(("127.0.0.1", server.port))
                    return True
                except OSError:
                    return False
                finally:
                    other.close()

            if second_bind_succeeds():
                raise AssertionError("another program could bind the viewer's port and receive its requests")
            try:
                ViewerServer(path, port=server.port).server_close()
                raise AssertionError("a second viewer was allowed to take the same port")
            except OSError:
                pass
            if os.name == "nt":  # control: show the hazard is real here without the protection
                a, b = socket.socket(), socket.socket()
                try:
                    for sock in (a, b):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    a.bind(("127.0.0.1", 0))
                    a.listen(1)
                    try:
                        b.bind(a.getsockname())
                        notes.append("control: an ordinary socket CAN share a port on this system, so the exclusive setting matters")
                    except OSError:
                        notes.append("control: this system does not let ordinary sockets share a port")
                finally:
                    a.close()
                    b.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
    return "; ".join(["listens on this computer only, refuses missing secret, wrong host name and writes, and its port cannot be shared"] + notes)


TRIPS = ("filter", "diff", "hook", "fsmonitor")  # the ways a repository's own config can make git run a program

# The program a hostile repository names. It records what started it (when, with which arguments, under which parent and grandparent command
# lines where the platform shows them) and then acts as a pass-through filter. If one ever fires where it must not, the failure can say who ran
# it instead of only that something did. Recording is best effort; the marker file itself always appears.
_TRIPWIRE = r'''import datetime, json, os, subprocess, sys

KEEP = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX", "GIT_EXEC_PATH", "GIT_REFLOG_ACTION")


def from_proc(pid):
    """(command line, parent pid) read from /proc, which Linux has; (None, None) where that cannot be read."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            command = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        with open("/proc/%d/stat" % pid, "rb") as handle:
            parent = int(handle.read().rsplit(b")", 1)[1].split()[1])
        return command, parent
    except (OSError, ValueError, IndexError):
        return None, None


def parse_ps(output):
    """(command line, parent pid) from the output of `ps -o ppid=,command= -p PID`, e.g. '  501 /usr/bin/git status'; (None, None) if it is not that."""
    parts = output.strip().split(None, 1)
    if len(parts) != 2:
        return None, None
    try:
        return parts[1].strip(), int(parts[0])
    except ValueError:
        return None, None


def from_ps(pid):
    """The same, asked of ps, which macOS and the BSDs have; (None, None) on Windows or when ps fails."""
    if os.name != "posix":
        return None, None
    try:
        shown = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, None
    return parse_ps(shown.stdout.decode("utf-8", "replace"))


def process(pid):
    """(command line, parent pid) of a process as far as this platform shows it, else (None, None)."""
    found = from_proc(pid)
    return found if found[0] is not None else from_ps(pid)


def clip(text, limit=300):
    """A long command line cut short, so it cannot crowd the rest of the record out of a failure message."""
    return text if text is None or len(text) <= limit else text[:limit] + "..."


def main():
    marker = sys.argv[1]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
    try:
        parent_command, grandparent_pid = process(os.getppid())
        record = {
            "time": now, "args": sys.argv[2:], "pid": os.getpid(), "ppid": os.getppid(),
            "parent": clip(parent_command), "grandparent": clip(process(grandparent_pid)[0]) if grandparent_pid else None,
            "cwd": os.getcwd(), "git_env": {name: os.environ[name] for name in KEEP if name in os.environ},
        }
    except Exception as exc:
        record = {"time": now, "args": sys.argv[2:], "recording_failed": repr(exc)}
    with open(marker, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    sys.stdout.buffer.write(sys.stdin.buffer.read())


if __name__ == "__main__":  # importing this file (a test does) only defines the functions
    main()
'''

_NOT_A_LEFTOVER = "The control phase used a separate repository and its own markers, so this is not a leftover from it."

_ORIGINAL = b"alpha\r\nbeta\r\n"  # Windows line endings: a snapshot's normalised text cannot reproduce these


@dataclass
class _Hostile:
    repo: Path
    markers: dict[str, Path]


def _write_tripwire(path: Path) -> Path:
    path.write_text(_TRIPWIRE, encoding="utf-8")
    return path


def _hostile_repo(git: str, root: Path, trap: Path, prefix: str, markers_dir: Path | None = None) -> _Hostile:
    """Build a repository whose own config names programs for git to run, each one the tripwire.

    The programs are a filter, an external diff, hooks and an fsmonitor. Each records into a marker file of its
    own. The repository holds `a.md` in two commits, the first with Windows line endings.

    Args:
        git: Path of the git executable.
        root: A new folder to create; the repository goes in `root/repo` and the hooks in `root/hooks`.
        trap: The tripwire script that every configured program points at.
        prefix: Names the markers, so two repositories made with different prefixes never share one.
        markers_dir: Where the markers go; `root` when omitted.

    Returns:
        The repository and its markers, keyed by the name in TRIPS.
    """
    root.mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    markers = {name: (markers_dir or root) / f"{prefix}-{name}" for name in TRIPS}
    python = Path(sys.executable).as_posix()

    def command(name: str) -> str:
        return f'"{python}" "{trap.as_posix()}" "{markers[name].as_posix()}" {name}'

    _git_run(git, repo, "init", "-q", "-b", "main")
    (repo / "a.md").write_bytes(_ORIGINAL)
    (repo / ".gitattributes").write_text("*.md filter=trap\n", encoding="utf-8")
    _git_run(git, repo, "add", "-A")
    _git_run(git, repo, "commit", "-q", "-m", "one")
    (repo / "a.md").write_bytes(b"changed\n")
    _git_run(git, repo, "add", "-A")
    _git_run(git, repo, "commit", "-q", "-m", "two")

    hooks = root / "hooks"
    hooks.mkdir()
    for hook in ("pre-commit", "post-commit", "reference-transaction", "post-index-change"):
        script = f"#!/bin/sh\n'{python}' '{trap.as_posix()}' '{markers['hook'].as_posix()}' hook-{hook} \"$@\" </dev/null\n"
        (hooks / hook).write_text(script, encoding="utf-8")
        (hooks / hook).chmod(0o755)
    for key, value in (("filter.trap.clean", command("filter")), ("filter.trap.smudge", command("filter")),
                       ("diff.external", command("diff")), ("core.fsmonitor", command("fsmonitor")),
                       ("core.hooksPath", str(hooks))):
        _git_run(git, repo, "config", key, value)
    return _Hostile(repo, markers)


def _run_control(git: str, hostile: _Hostile) -> list[str]:
    """CONTROL: with ordinary git, each of these traps fires on this machine. Returns the ones that did."""
    raw = _clean_env()
    cwd = str(hostile.repo)
    subprocess.run([git, "hash-object", "a.md"], cwd=cwd, env=raw, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    subprocess.run([git, "log", "-p", "--ext-diff", "--no-color"], cwd=cwd, env=raw, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    _git_run(git, hostile.repo, "commit", "-q", "--allow-empty", "-m", "control")
    return [name for name, marker in hostile.markers.items() if marker.exists()]


def _git_version_line(git: str) -> str:
    try:
        out = subprocess.run([git, "--version"], capture_output=True, timeout=30, env=_clean_env(), stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return "git --version: unavailable"
    return "git --version: " + safe_text(out.decode("utf-8", "replace").strip() or "no output", 80)


def _tripwire_report(markers: dict[str, Path], names: list[str]) -> str:
    """What each tripwire that fired recorded: its calls, shortest useful form, safe to print."""
    parts = []
    for name in names:
        try:
            lines = [line for line in markers[name].read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        except OSError:
            lines = ["(the marker could not be read)"]
        shown = " | ".join(safe_text(line, 1200) for line in lines[:3]) if lines else "(empty)"
        parts.append(f"[{name}] {len(lines)} call(s): {shown}")
    return "; ".join(parts)


def check_rollback_is_safe() -> str:
    """Prove that a git rollback in a hostile repository runs no program and restores the exact bytes.

    Control: in one hostile repository ordinary git fires the tripwires (SKIP if neither the filter nor the diff
    does). Then a rollback is applied in a second, identical repository with a separate set of markers, and none
    may fire. It must also restore the original bytes with their CRLF line endings, as one new commit by
    `memdebug`, and save the uncommitted edit it replaced first.
    """
    from .adapters.restore import Restorer

    git = _need_git()
    with _tmp() as base:
        base_path = Path(base)
        trap = _write_tripwire(base_path / "trap.py")
        # Two separate repositories with separate markers. The control makes ordinary git run the tripwires in ITS repository, and anything it
        # leaves behind (a late process, a lingering write) can only ever reach ITS markers. The protected rollback gets a repository that
        # ordinary git has never run in, and is judged by its own markers alone.
        control = _hostile_repo(git, base_path / "control", trap, "CONTROL")
        protected = _hostile_repo(git, base_path / "protected", trap, "PWNED")
        armed = _run_control(git, control)
        if "filter" not in armed and "diff" not in armed:
            raise _Skip("could not make a hostile repository run a program here, so the protection is unproven")
        repo, markers, original = protected.repo, protected.markers, _ORIGINAL

        ledger = Ledger(base_path / "ledger.db")
        scope = {"store": "selftest"}
        info = ledger.save_snapshot("markdown-git", scope, [Memory(id="a.md", text="alpha\nbeta\n", scope=scope)], complete=True,
                                    taken_at=datetime.now(timezone.utc))
        (repo / "a.md").write_bytes(b"precious unsaved work\n")  # an uncommitted edit the rollback will replace
        adapter = MarkdownGitAdapter(repo, store="selftest")
        restorer = Restorer(adapter)
        snapshot = ledger.load_snapshot(info.id)
        plan = restorer.plan(snapshot)
        outcome = restorer.apply(snapshot, expected_plan_id=plan.plan_id)

        ran = [name for name, marker in markers.items() if marker.exists()]
        if ran:
            raise AssertionError("a program from the repository ran during the rollback: " + ", ".join(ran)
                                 + ". What started it: " + _tripwire_report(markers, ran) + f". {_git_version_line(git)}. {_NOT_A_LEFTOVER}")
        if (repo / "a.md").read_bytes() != original:
            raise AssertionError("the original bytes (with their line endings) were not restored exactly")
        code, author = adapter.git.run_small(["log", "-1", "--format=%an"])
        code2, saved = adapter.git.run_small(["show", f"{outcome.backup_ref}:a.md"])
        if code != 0 or author.strip() != b"memdebug" or outcome.commit is None:
            raise AssertionError("the rollback did not make one new commit")
        if outcome.backup_ref is None or code2 != 0 or saved != b"precious unsaved work\n":
            raise AssertionError("the content the rollback replaced was not saved first")
    return ("restored exact bytes in a new commit, saved the replaced edit first, and ran no hook, filter or helper program; "
            f"control: {', '.join(armed)} do fire without the protection")



def check_folder_rollback_is_safe() -> str:
    """Prove that the plain-folder rollback is safe on this machine, without needing git.

    There is no control. Checks that a backup folder inside the notes is refused, that planning writes nothing,
    that the file comes back byte for byte (CRLF kept), that what it replaced is saved first byte for byte, that
    nothing outside the plan is touched, and that a plan which no longer matches is refused.
    """
    from .adapters.folder import FolderAdapter
    from .adapters.folder_restore import FolderRestorer
    from .errors import RestoreError

    with _tmp() as base:
        root = Path(base)
        notes, outside = root / "notes", root / "outside.md"
        notes.mkdir()
        outside.write_bytes(b"not part of the notes\n")
        original = b"alpha\r\nbeta\r\n"  # Windows line endings, which a snapshot's normalised text cannot hold
        (notes / "a.md").write_bytes(original)
        adapter = FolderAdapter(notes, store="selftest")
        scope = {"store": "selftest"}
        ledger = Ledger(root / "ledger.db")
        live = adapter.list_memories(scope)
        info = ledger.save_snapshot("folder", scope, live.memories, complete=live.complete, taken_at=datetime.now(timezone.utc))
        precious = b"precious unsaved work\r\n\xff"
        (notes / "a.md").write_bytes(precious)

        backups = root / "backups"
        snapshot = ledger.load_snapshot(info.id)
        inside = FolderRestorer(adapter, notes / "backups").plan(snapshot)
        if not inside.blockers:
            raise RuntimeError("a backup folder inside the notes was not refused")
        restorer = FolderRestorer(adapter, backups)
        before = (notes / "a.md").read_bytes()
        plan = restorer.plan(snapshot)
        if (notes / "a.md").read_bytes() != before or backups.exists():
            raise RuntimeError("planning changed something")
        outcome = restorer.apply(snapshot, expected_plan_id=plan.plan_id)
        if (notes / "a.md").read_bytes() != original:
            raise RuntimeError("the file did not come back as it was")
        if outcome.backup_path is None or (Path(outcome.backup_path) / "files" / "a.md").read_bytes() != precious:
            raise RuntimeError("the replaced edit was not saved byte for byte")
        if outside.read_bytes() != b"not part of the notes\n" or sorted(p.name for p in notes.iterdir()) != ["a.md"]:
            raise RuntimeError("something outside the plan was changed")
        try:
            restorer.apply(snapshot, expected_plan_id="0" * 64)
        except RestoreError:
            pass
        else:
            raise RuntimeError("a plan that no longer matched was applied")
    return "the plan wrote nothing, the file came back (CRLF kept), the replaced edit was saved byte for byte first, a backup folder inside the notes was refused, and nothing else changed"


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("git ignores global config", check_git_ignores_global_config),
    ("hostile repository config cannot run programs", check_hostile_repo_config_cannot_run_programs),
    ("history file is opened read-only", check_history_file_is_read_only),
    ("a hung process tree is killed", check_hung_process_tree_is_killed),
    ("symlinks are not followed", check_symlinks_are_not_followed),
    ("directory junctions are not followed", check_junctions_are_not_followed),
    ("dangerous file names are rejected", check_dangerous_names_are_rejected),
    ("console survives unencodable text", check_console_survives_unencodable_text),
    ("ledger tamper detection", check_ledger_tamper_detection),
    ("ledger file permissions", check_ledger_file_permissions),
    ("viewer is local-only and protected", check_viewer_is_local_and_protected),
    ("rollback is safe", check_rollback_is_safe),
    ("folder rollback is safe", check_folder_rollback_is_safe),
]


def run_all() -> list[Result]:
    """Run every check in CHECKS and return the environment line followed by one result per check.

    A check that raises `_Skip` is reported as SKIP, an `AssertionError` as FAIL with its message, and any other
    exception as FAIL (only its type name is kept), because a check that cannot run is not a pass.
    """
    results: list[Result] = [("environment", "INFO", check_info())]
    for name, check in CHECKS:
        try:
            results.append((name, "PASS", check()))
        except _Skip as skip:
            results.append((name, "SKIP", str(skip)))
        except AssertionError as failure:
            results.append((name, "FAIL", str(failure)))
        except Exception as exc:  # a check that cannot even run is not a pass
            results.append((name, "FAIL", f"could not run: {type(exc).__name__}"))
    return results
