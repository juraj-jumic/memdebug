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
from .textsafe import console_safe

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
    git = find_git()
    version = _git_version(git) if git else None
    return (f"{sys.platform}, python {sys.version.split()[0]}, "
            f"git {'.'.join(map(str, version)) if version else 'not found'}"
            + (f" at {git}" if git else ""))


def check_git_ignores_global_config() -> str:
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


def check_hostile_repo_config_cannot_run_programs() -> str:
    git = _need_git()
    with _tmp() as base:
        base_path = Path(base)
        marker = base_path / "PWNED"
        script = base_path / "evil.py"
        script.write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
        command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
        repo = base_path / "repo"
        repo.mkdir()
        _git_run(git, repo, "init", "-q", "-b", "main")
        for version in ("one", "two"):
            (repo / "a.md").write_text(version + "\n", encoding="utf-8")
            _git_run(git, repo, "add", "-A")
            _git_run(git, repo, "commit", "-q", "-m", version)
        for key in ("diff.external", "core.fsmonitor", "core.pager", "pager.log", "core.editor"):
            _git_run(git, repo, "config", key, command)
        _git_run(git, repo, "config", "core.hooksPath", str(base_path))

        subprocess.run([git, "log", "-p", "--ext-diff", "--no-color"], cwd=str(repo), env=_clean_env(),
                       capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        if not marker.exists():
            raise _Skip("could not make a hostile config run a program here, so the protection is unproven")
        marker.unlink()

        adapter = MarkdownGitAdapter(repo, store="selftest")
        adapter.read_history(100)
        adapter.history("a.md")
        adapter.list_memories({"store": "selftest"})
        if marker.exists():
            raise AssertionError("a program named in the repository's config was executed")
    return "a repository config that runs programs did not run any"


def check_history_file_is_read_only() -> str:
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
    bad = ["NUL.md", "con.md", "aux/x.md", "notes:hidden.md", "C:evil.md", "a.md.", "dir /x.md", ".GIT/x.md", "GIT~1/x.md",
           "a<b>.md", 'q"uote.md', "../x.md", "/abs.md", "COM1.md", "lpt9.txt.md", "a\\b.md", "x\ny.md"]
    good = ["ok/fine.md", "caf\u00e9.md", "People/Ann.md", "-rf.md"]
    wrongly_ok = [p for p in bad if _valid_relpath(p, (".md",)) is not None]
    wrongly_bad = [p for p in good if _valid_relpath(p, (".md",)) is None]
    if wrongly_ok or wrongly_bad:
        raise AssertionError(f"accepted {wrongly_ok!r}; rejected {wrongly_bad!r}")
    return f"{len(bad)} dangerous names rejected, {len(good)} normal names accepted"


def check_console_survives_unencodable_text() -> str:
    for encoding in ("cp1252", "ascii", "cp437"):
        shown = console_safe("\u65e5\u672c\u8a9e \U0001f600 caf\u00e9", encoding)
        shown.encode(encoding)  # must be representable
        if "\u65e5" in shown:
            raise AssertionError("characters outside the console encoding were not escaped")
    return "text a console cannot show is escaped instead of crashing"


def check_ledger_tamper_detection() -> str:
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


def check_rollback_is_safe() -> str:
    from .adapters.restore import Restorer

    git = _need_git()
    with _tmp() as base:
        base_path = Path(base)
        repo = base_path / "repo"
        repo.mkdir()
        trap = base_path / "trap.py"
        trap.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('x', encoding='utf-8')\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n",
                        encoding="utf-8")
        markers = {name: base_path / f"PWNED-{name}" for name in ("filter", "diff", "hook", "fsmonitor")}

        def command(name: str) -> str:
            return f'"{Path(sys.executable).as_posix()}" "{trap.as_posix()}" "{markers[name].as_posix()}"'

        _git_run(git, repo, "init", "-q", "-b", "main")
        original = b"alpha\r\nbeta\r\n"  # Windows line endings: a snapshot's normalised text cannot reproduce these
        (repo / "a.md").write_bytes(original)
        (repo / ".gitattributes").write_text("*.md filter=trap\n", encoding="utf-8")
        _git_run(git, repo, "add", "-A")
        _git_run(git, repo, "commit", "-q", "-m", "one")
        (repo / "a.md").write_bytes(b"changed\n")
        _git_run(git, repo, "add", "-A")
        _git_run(git, repo, "commit", "-q", "-m", "two")

        hooks = base_path / "hooks"
        hooks.mkdir()
        for hook in ("pre-commit", "post-commit", "reference-transaction", "post-index-change"):
            (hooks / hook).write_text(f"#!/bin/sh\ntouch '{markers['hook'].as_posix()}'\n", encoding="utf-8")
            (hooks / hook).chmod(0o755)
        for key, value in (("filter.trap.clean", command("filter")), ("filter.trap.smudge", command("filter")),
                           ("diff.external", command("diff")), ("core.fsmonitor", command("fsmonitor")),
                           ("core.hooksPath", str(hooks))):
            _git_run(git, repo, "config", key, value)

        # CONTROL: with ordinary git, each of these traps fires on this machine.
        raw = _clean_env()
        subprocess.run([git, "hash-object", "a.md"], cwd=str(repo), env=raw, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        subprocess.run([git, "log", "-p", "--ext-diff", "--no-color"], cwd=str(repo), env=raw, capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL)
        _git_run(git, repo, "commit", "-q", "--allow-empty", "-m", "control")
        armed = [name for name, marker in markers.items() if marker.exists()]
        if "filter" not in armed and "diff" not in armed:
            raise _Skip("could not make a hostile repository run a program here, so the protection is unproven")
        for marker in markers.values():
            marker.unlink(missing_ok=True)

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
            raise AssertionError("a program from the repository ran during the rollback: " + ", ".join(ran))
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
    """The plain-folder rollback, proven on this machine (no git needed): the plan writes nothing, the file comes back, what it replaced is
    saved first byte for byte, a backup folder inside the notes is refused, and nothing outside the notes is touched."""
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
