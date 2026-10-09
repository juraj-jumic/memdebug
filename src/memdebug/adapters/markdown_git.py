"""Read-only adapter for agent memory kept as markdown files in a git repository.

One markdown file is one memory; its id is its path relative to the repository top.

  * live memories -> the files in the working tree (what the agent actually reads)
  * history       -> `git log` of the checked-out branch, first-parent, newest last
  * "outside the API" -> an uncommitted edit, addition or deletion in the working tree

The repository is UNTRUSTED. A repo carries its own config, which can make git run programs,
and its tree can hold symlinks aimed at your secrets. So:

  * git runs with a scrubbed environment and the repo's risky settings overridden on the command
    line (no fsmonitor, no hooks, no signature checking, no pager, no external diff, no textconv,
    no replace-refs, no global or system config);
  * only read-only git commands are used, with fixed argument lists and no shell;
  * every git process has a wall-clock limit, an output cap and a stderr cap;
  * history is read from git objects by id, never from files on disk;
  * the working tree is read with symlinks refused and non-regular files skipped;
  * paths with control characters, quotes, backslashes, `..` or `.git` parts are skipped.

This adapter never writes to the repository. (Restoring is done by restore.py, which is a separate, explicit step.)
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Callable, Iterator

from .. import longpath
from ..errors import AdapterError
from ..models import MAX_ID_CHARS, Memory, MemoryEvent, Op, Source
from ..textsafe import bound_text, has_unsafe_chars, safe_text
from .base import HistoryRead, LiveMemories
from .common import Warnings, clean_id, parse_ts

MAX_FILE_BYTES = 1_048_576
MAX_WALK_DEPTH = 32
_MAX_LINE = 65_536
_MAX_LOG_BYTES = 256 * 1024 * 1024
_SMALL_OUTPUT = 65_536
_SHA = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_SHA_RE = re.compile(rf"^{_SHA}\Z")
_RAW_RE = re.compile(rf"^:(\d{{6}}) (\d{{6}}) ({_SHA}) ({_SHA}) ([A-Z])\t(.+)\Z")
_REGULAR_MODES = {"100644", "100755"}
_STATUS_OPS = {"A": Op.ADD, "M": Op.UPDATE, "D": Op.DELETE}
_MIN_GIT = (2, 31)  # --diff-merges=first-parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_git() -> str | None:
    """Finds git on PATH, ignoring the current folder.

    Windows (and an empty or '.' PATH entry on POSIX) would otherwise run a git.exe planted in whatever folder you
    happen to be in. Only absolute PATH entries are searched.

    Returns:
        The absolute path of the first executable git found, or None if there is none.
    """
    names = ["git.exe"] if os.name == "nt" else ["git"]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = directory.strip().strip('"')
        if not directory or not os.path.isabs(directory):
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _too_large(size: int) -> str:
    return f"[file too large to read: {size} bytes]"


def _text_from_bytes(data: bytes) -> str:
    # Line endings are normalised so a checkout with CRLF does not look like an edit.
    return bound_text(data.decode("utf-8", "replace").replace("\r\n", "\n"))


_RESERVED_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{base}{n}" for base in ("com", "lpt") for n in range(1, 10)
}
_FORBIDDEN_CHARS = set('"\\:*?<>|')  # quotes git adds, NTFS streams and drive letters, wildcards


def _bad_component(part: str) -> bool:
    """Tells whether a path component is dangerous on some platform.

    Applied everywhere, so a repository looks the same from every operating system. Covers empty, "." and ".."
    parts, trailing dots or spaces, the git folder and its NTFS short name, and Windows device names.
    """
    if part in ("", ".", ".."):
        return True
    folded = part.casefold()
    if folded != folded.rstrip(" ."):  # Windows silently drops trailing dots and spaces
        return True
    if folded == ".git" or folded.startswith("git~"):  # the git folder, and its NTFS short name
        return True
    return folded.split(".")[0] in _RESERVED_NAMES  # CON, NUL.md, com1.txt are devices on Windows


def _is_reparse_point(info: os.stat_result) -> bool:
    """Windows junctions and other reparse points; they are not reported as symlinks."""
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _valid_relpath(path: str, suffixes: tuple[str, ...]) -> str | None:
    """Returns a repository-relative path that is safe to show, store and compare; else None.

    git quotes names with a double quote or backslash, so refusing them everywhere keeps the
    history and the working-tree listing in agreement. A path is also refused when it is too long, is
    absolute, has control characters or other forbidden characters, has a component rejected by `_bad_component`,
    or does not end in one of `suffixes` (compared ignoring case).
    """
    if not path or len(path) > MAX_ID_CHARS or path[0] == "/" or has_unsafe_chars(path):
        return None
    if any(ch in _FORBIDDEN_CHARS for ch in path):
        return None
    if any(_bad_component(part) for part in path.split("/")):
        return None
    if not path.lower().endswith(suffixes):
        return None
    return path


def _clean_env() -> dict[str, str]:
    env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ASKPASS": "false",
        "LC_ALL": "C",
    }
    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _spawn_flags() -> dict:
    """Own process group on POSIX, no console window on Windows."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": _NO_WINDOW}


class _Run:
    """One git child process with a hard time limit."""

    def __init__(self, proc: subprocess.Popen, errfile: IO[bytes], timeout: float):
        self.proc = proc
        self._errfile = errfile
        self.timed_out = False
        self.stopped = False  # we ended it on purpose (a limit was reached)
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    @property
    def out(self) -> IO[bytes]:
        """The child's output pipe. It is always started with one; this makes that checkable and typed."""
        stream = self.proc.stdout
        if stream is None:
            raise AdapterError("git has no output stream")
        return stream

    @property
    def inp(self) -> IO[bytes]:
        stream = self.proc.stdin
        if stream is None:
            raise AdapterError("git was started without an input stream")
        return stream

    def _expire(self) -> None:
        self.timed_out = True
        self.stop()

    def stop(self) -> None:
        """Kill git and anything it started (a helper holding the pipe would block us)."""
        self.stopped = True
        if self.proc.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(self.proc.pid, signal.SIGKILL)
            else:
                # taskkill /T ends the whole tree. Called by absolute path, never via PATH.
                taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
                try:
                    subprocess.run([taskkill, "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True, timeout=10, creationflags=_NO_WINDOW)
                except (OSError, subprocess.SubprocessError):
                    pass
                self.proc.kill()
        except OSError:
            pass

    def stderr_text(self) -> str:
        try:
            self._errfile.seek(0)
            return self._errfile.read(2048).decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""

    def close(self) -> None:
        self._timer.cancel()
        if self.proc.returncode is None:
            self.stop()
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
        self.proc.wait()
        self._errfile.close()


class _Git:
    def __init__(self, git_path: str, root: Path, timeout: float):
        self.git_path = git_path
        self.root = root
        self.timeout = timeout

    def _command(self, args: list[str]) -> list[str]:
        null = os.devnull
        return [
            self.git_path, "--no-pager", "--literal-pathspecs",
            "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={null}", "-c", f"core.attributesFile={null}",
            "-c", "log.showSignature=false", "-c", "gpg.program=false",
            *args,
        ]

    @contextmanager
    def spawn(self, args: list[str], *, interactive: bool = False, plain: bool = False,
              env: dict[str, str] | None = None) -> Iterator[_Run]:
        errfile = tempfile.TemporaryFile()
        command = [self.git_path, *args] if plain else self._command(args)
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=errfile,
                env={**_clean_env(), **(env or {})}, cwd=str(self.root), shell=False,
                **_spawn_flags(),
            )
        except OSError as exc:
            errfile.close()
            raise AdapterError(f"cannot run git: {exc.strerror}") from exc
        run = _Run(proc, errfile, self.timeout)
        try:
            yield run
        finally:
            run.close()

    def run_small(self, args: list[str], *, plain: bool = False) -> tuple[int, bytes]:
        with self.spawn(args, plain=plain) as run:
            data = run.out.read(_SMALL_OUTPUT + 1)
            if len(data) > _SMALL_OUTPUT:
                run.stop()
                raise AdapterError("git produced unexpectedly large output")
            code = run.proc.wait()
            if run.timed_out:
                raise AdapterError(f"git timed out after {self.timeout:g} seconds")
            if code != 0:
                return code, run.stderr_text().encode()
            return code, data

    def finish(self, run: _Run) -> None:
        """Call after reading a stream to the end; raises if git failed or timed out."""
        code = run.proc.wait()
        if run.timed_out:
            raise AdapterError(f"git timed out after {self.timeout:g} seconds")
        if code != 0 and not run.stopped:
            raise AdapterError(f"git failed: {safe_text(run.stderr_text(), 200)}")


def _read_lines(stream: IO[bytes], run: _Run) -> Iterator[bytes | None]:
    """Lines from git, with bounded line length and total size. None marks an overlong line."""
    total = 0
    while True:
        chunk = stream.readline(_MAX_LINE)
        if not chunk:
            return
        total += len(chunk)
        if total > _MAX_LOG_BYTES:
            run.stop()
            return
        if len(chunk) == _MAX_LINE and not chunk.endswith(b"\n"):
            while True:  # drain the rest of the overlong line
                more = stream.readline(_MAX_LINE)
                total += len(more)
                if not more or more.endswith(b"\n"):
                    break
            yield None
            continue
        yield chunk.rstrip(b"\n")


class _BlobReader:
    """Reads blobs by object id through two long-lived `git cat-file` processes."""

    def __init__(self, git: _Git):
        self._git = git
        self._cache: dict[str, str | None] = {}
        self._stack = ExitStack()

    def __enter__(self) -> "_BlobReader":
        self._check = self._stack.enter_context(self._git.spawn(["cat-file", "--batch-check"], interactive=True))
        self._batch = self._stack.enter_context(self._git.spawn(["cat-file", "--batch"], interactive=True))
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    @property
    def timed_out(self) -> bool:
        return self._check.timed_out or self._batch.timed_out

    @staticmethod
    def _ask(run: _Run, sha: str) -> bytes:
        run.inp.write(f"{sha}\n".encode())  # sha was validated as pure hex
        run.inp.flush()
        return run.out.readline(256)

    def raw(self, sha: str) -> bytes | None:
        """The exact bytes of a blob, or None if it cannot be read or is larger than MAX_FILE_BYTES."""
        if not (_SHA_RE.match(sha) and set(sha) != {"0"}):
            return None
        try:
            parts = self._ask(self._check, sha).decode("ascii", "replace").split()
            if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit():
                size = int(parts[2])
                if size > MAX_FILE_BYTES:
                    return None
                header = self._ask(self._batch, sha).decode("ascii", "replace").split()
                if len(header) == 3 and header[1] == "blob" and header[2] == str(size):
                    data = self._batch.out.read(size)
                    if len(data) == size and self._batch.out.read(1) == b"\n":
                        return data
        except (OSError, ValueError):
            return None
        return None

    def lookup(self, commit: str, path: str) -> str | None:
        """The object id of the file `path` as it was in `commit`, or None if it did not exist there."""
        if not _SHA_RE.match(commit) or not path or any(ch in path for ch in "\n\r\0"):
            return None
        try:
            parts = self._ask(self._check, f"{commit}:{path}").decode("utf-8", "replace").split()
        except (OSError, ValueError):
            return None
        return parts[0] if len(parts) == 3 and parts[1] == "blob" and _SHA_RE.match(parts[0]) else None

    def text(self, sha: str) -> str | None:
        """Text of the blob, a size marker if it is too big, or None if it cannot be read."""
        if sha in self._cache:
            return self._cache[sha]
        result: str | None = None
        if _SHA_RE.match(sha) and set(sha) != {"0"}:
            try:
                parts = self._ask(self._check, sha).decode("ascii", "replace").split()
                if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit():
                    size = int(parts[2])
                    if size > MAX_FILE_BYTES:
                        result = _too_large(size)
                    else:
                        header = self._ask(self._batch, sha).decode("ascii", "replace").split()
                        if len(header) == 3 and header[1] == "blob" and header[2] == str(size):
                            data = self._batch.out.read(size)
                            if len(data) == size and self._batch.out.read(1) == b"\n":
                                result = _text_from_bytes(data)
            except (OSError, ValueError):
                result = None
        self._cache[sha] = result
        return result


@dataclass(frozen=True)
class _Change:
    commit: str
    ts: datetime
    observed: bool
    author: str | None
    path: str
    status: str
    old_sha: str
    new_sha: str

    @property
    def ref(self) -> str:
        return hashlib.sha256(f"{self.commit}\x00{self.path}".encode()).hexdigest()


class MarkdownGitAdapter:
    """Read-only adapter for markdown memory files kept in a git repository.

    A file's id is its path relative to the repository top, and the store's scope is `{"store": <name>}`. Live
    memories come from the working tree and history from `git log` (see the module docstring for the hardening
    applied to the untrusted repository). The constructor runs git for a version check and to check that `root` is
    the repository top.

    Raises:
        AdapterError: From the constructor, if the path is not the top of a usable git repository, git is missing,
            too old or not a real executable, a limit or the suffixes are invalid, or `subdir` is not a plain existing
            folder inside the repository.
    """

    name = "markdown-git"
    capabilities = {"history", "global_feed"}

    def __init__(
        self,
        root: str | Path,
        *,
        store: str | None = None,
        subdir: str | None = None,
        suffixes: tuple[str, ...] = (".md",),
        max_files: int = 20_000,
        git_timeout: float = 120.0,
        max_total_chars: int = 100_000_000,
        git_path: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        try:
            self._root = longpath.resolve(root, strict=True)
        except OSError as exc:
            raise AdapterError(f"cannot access repository path: {exc.strerror}") from exc
        if not longpath.isdir(self._root):
            raise AdapterError("repository path is not a directory")
        if not (isinstance(max_files, int) and max_files > 0 and git_timeout > 0 and max_total_chars > 0):
            raise AdapterError("max_files, git_timeout and max_total_chars must be positive")
        if not suffixes or any(not s.startswith(".") or s != s.lower() for s in suffixes):
            raise AdapterError("suffixes must be lowercase and start with a dot, such as '.md'")
        self._suffixes = tuple(suffixes)
        self._store = clean_id(store) or clean_id(self._root.name) or "memory"
        self._max_files = max_files
        self._max_total_chars = max_total_chars
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        found = git_path or find_git()
        if not found:
            raise AdapterError("git was not found; install git 2.31 or newer")
        if not os.path.isabs(found) or not os.path.isfile(found):
            raise AdapterError("the git path must be an absolute path to an executable file")
        if os.name == "nt" and not found.lower().endswith(".exe"):
            raise AdapterError("on Windows the git path must be a real git.exe, not a script or shim")
        self._git = _Git(found, self._root, git_timeout)
        self._check_git_version()
        self._subdir = self._validate_subdir(subdir)
        self._check_repository()

    @property
    def store(self) -> str:
        """The store name used in scopes, snapshots and events."""
        return self._store

    # Read-only views for restore.py, which lives beside this adapter and shares its hardening.
    @property
    def root(self) -> Path:
        """The resolved path of the repository top (or, for a plain folder, of the folder)."""
        return self._root

    @property
    def git(self) -> "_Git":
        """The hardened runner for git commands, shared with the rollback engine."""
        return self._git

    @property
    def subdir(self) -> str | None:
        """The folder inside the repository this store is limited to, or None for the whole repository."""
        return self._subdir

    @property
    def suffixes(self) -> tuple[str, ...]:
        """The lowercase file name endings that count as memory files, such as ".md"."""
        return self._suffixes

    # -- setup checks -------------------------------------------------------------------------

    def _check_git_version(self) -> None:
        _, out = self._git.run_small(["--version"], plain=True)
        match = re.search(rb"git version (\d+)\.(\d+)", out)
        if not match or (int(match.group(1)), int(match.group(2))) < _MIN_GIT:
            raise AdapterError("git 2.31 or newer is required")

    def _validate_subdir(self, subdir: str | None) -> str | None:
        if subdir is None:
            return None
        value = subdir.strip("/")
        parts = value.split("/")
        if (not value or len(value) > MAX_ID_CHARS or has_unsafe_chars(value)
                or any(ch in _FORBIDDEN_CHARS for ch in value) or any(_bad_component(p) for p in parts)):
            raise AdapterError("subdir must be a plain relative path inside the repository")
        target = self._root.joinpath(*parts)
        if longpath.islink(target) or not longpath.isdir(target):
            raise AdapterError("subdir does not exist or is not a plain directory")
        return value

    def _check_repository(self) -> None:
        code, out = self._git.run_small(["rev-parse", "--show-toplevel"])
        if code != 0:
            detail = safe_text(out.decode("utf-8", "replace"), 200)
            hint = " (git refuses repositories owned by another user)" if "dubious" in detail else ""
            raise AdapterError(f"not a usable git repository: {detail}{hint}")
        try:
            top = longpath.resolve(out.decode("utf-8", "replace").strip())
        except OSError as exc:
            raise AdapterError("cannot resolve the repository top") from exc
        if top != self._root:
            raise AdapterError("the path must be the top of the git repository, not a folder inside it")

    # -- history ------------------------------------------------------------------------------

    def read_history(self, max_rows: int) -> HistoryRead:
        """Reads the first-parent history of HEAD, oldest change first, as one event per file change.

        Runs read-only git commands and reads file versions from git objects, never from the working tree. Only
        changes to files with a memory-file name inside `subdir` become events; renames are reported as a delete plus
        an add. A repository with no commits yields an empty history. The result is marked truncated when `max_rows`
        changes or the text budget (`max_total_chars`) was reached.

        Raises:
            AdapterError: If `max_rows` is not positive, or git fails or times out.
        """
        if not (isinstance(max_rows, int) and max_rows > 0):
            raise AdapterError("max_rows must be a positive integer")
        warnings = Warnings()
        now = self._clock()
        code, _ = self._git.run_small(["rev-parse", "--verify", "-q", "HEAD"])
        if code != 0:  # a repository with no commits yet
            return HistoryRead(events=[], refs=set(), truncated=False)

        args = [
            "log", "--topo-order", "--reverse", "--first-parent", "--diff-merges=first-parent",
            "--no-renames", "--raw", "--no-abbrev", "--no-color", "--no-ext-diff", "--no-textconv",
            "--format=%x01%H%x02%cI%x02%an", "HEAD", "--",
        ]
        if self._subdir:
            args.append(self._subdir)

        changes: list[_Change] = []
        refs: set[str] = set()
        truncated = False
        current: tuple[str, datetime, bool, str | None] | None = None
        with self._git.spawn(args) as run:
            for line in _read_lines(run.out, run):
                if line is None:
                    warnings.add("git log produced an overlong line; skipped")
                    continue
                text = line.decode("utf-8", "replace")
                if text.startswith("\x01"):
                    current = self._parse_header(text, now, warnings)
                elif text.startswith(":"):
                    if current is None:
                        continue
                    change = self._parse_change(text, current, warnings)
                    if change is None:
                        continue
                    if len(changes) >= max_rows:
                        truncated = True
                        run.stop()
                        break
                    changes.append(change)
                    refs.add(change.ref)
                elif text:
                    warnings.add("unexpected output from git log; line skipped")
            if run.timed_out:
                raise AdapterError(f"git timed out after {self._git.timeout:g} seconds")
            if not truncated:
                self._git.finish(run)

        events, skipped, over_budget = self._events_from(changes, warnings)
        if over_budget:
            truncated = True
        if truncated:
            warnings.add("history was only partly read (row limit or memory budget reached)")
        return HistoryRead(events=events, refs=refs, truncated=truncated, skipped=skipped, warnings=warnings.as_list())

    def _parse_header(self, text: str, now: datetime, warnings: Warnings) -> tuple[str, datetime, bool, str | None] | None:
        parts = text[1:].split("\x02")
        if len(parts) != 3 or not _SHA_RE.match(parts[0]):
            warnings.add("unreadable commit header; its changes were skipped")
            return None
        ts, observed = parse_ts(parts[1]), False
        if ts is None:
            ts, observed = now, True
            warnings.add(f"commit {parts[0][:12]}: no usable date; using the time it was read")
        elif ts > now + timedelta(days=1):
            warnings.add(f"commit {parts[0][:12]}: date is in the future")
        return (parts[0], ts, observed, clean_id(parts[2]))

    def _parse_change(self, text: str, header: tuple[str, datetime, bool, str | None], warnings: Warnings) -> _Change | None:
        match = _RAW_RE.match(text)
        if not match:
            warnings.add("unreadable change line from git; skipped")
            return None
        src_mode, dst_mode, old_sha, new_sha, status, path = match.groups()
        quoted = path.startswith('"')
        if not path.rstrip('"').lower().endswith(self._suffixes):
            return None  # not a memory file at all; nothing to say
        relpath = None if quoted else _valid_relpath(path, self._suffixes)
        if relpath is None:
            warnings.add(f"path with unsafe or unsupported characters skipped: {safe_text(path, 60)}")
            return None
        if status not in _STATUS_OPS:
            warnings.add(f"unsupported change type {status} for {safe_text(relpath, 60)}; skipped")
            return None
        modes = {"A": [dst_mode], "D": [src_mode], "M": [src_mode, dst_mode]}[status]
        if any(m not in _REGULAR_MODES for m in modes):
            warnings.add(f"{safe_text(relpath, 60)} is a symlink or submodule; not followed")
            return None
        commit, ts, observed, author = header
        return _Change(commit, ts, observed, author, relpath, status, old_sha, new_sha)

    def _events_from(self, changes: list[_Change], warnings: Warnings) -> tuple[list[MemoryEvent], int, bool]:
        events: list[MemoryEvent] = []
        skipped = 0
        total = 0
        over_budget = False
        scope = {"store": self._store}
        with _BlobReader(self._git) as blobs:
            for change in changes:
                before = after = None
                if change.status in ("M", "D"):
                    before = blobs.text(change.old_sha)
                    if before is None:
                        skipped += 1
                        warnings.add(f"{safe_text(change.path, 60)}: earlier version is not available; skipped")
                        continue
                if change.status in ("A", "M"):
                    after = blobs.text(change.new_sha)
                    if after is None:
                        skipped += 1
                        warnings.add(f"{safe_text(change.path, 60)}: version is not available; skipped")
                        continue
                if change.status == "M" and before == after:
                    continue  # only the file mode changed
                total += len(before or "") + len(after or "")
                if total > self._max_total_chars:
                    over_budget = True
                    break
                try:
                    events.append(MemoryEvent(
                        backend=self.name, memory_id=change.path, op=_STATUS_OPS[change.status],
                        ts=change.ts, ts_observed=change.observed, backend_ref=change.ref, scope=scope,
                        before=before, after=after,
                        source=Source(actor_id=change.author) if change.author else None,
                    ))
                except ValueError:
                    skipped += 1
                    warnings.add(f"{safe_text(change.path, 60)}: failed validation; skipped")
            if blobs.timed_out:
                raise AdapterError(f"git timed out after {self._git.timeout:g} seconds")
        return events, skipped, over_budget

    # -- live listing (the working tree) -----------------------------------------------------------------

    def list_memories(self, scope: dict[str, str]) -> LiveMemories:
        """Lists the memory files in the working tree, which is what the agent reads (uncommitted edits included).

        Reads files directly, without git. Links and junctions are never followed, and anything that is not a plain
        regular file, a folder or file with an unsafe name, and folders deeper than MAX_WALK_DEPTH are skipped (the
        `.git` folder is skipped silently). Every other skip is reported as a warning and marks the listing
        incomplete, as does reaching `max_files`.
        A file over MAX_FILE_BYTES is listed with a "too large" marker instead of its text, and line endings are
        normalised to LF.

        Raises:
            AdapterError: If `scope` is not `{"store": <this store's name>}`.
        """
        if scope != {"store": self._store}:
            raise AdapterError(f"scope must be {{'store': {safe_text(self._store, 40)!r}}} for this repository")
        warnings = Warnings()
        memories: list[Memory] = []
        state = {"complete": True}

        def walk_error(exc: OSError) -> None:
            state["complete"] = False  # an unreadable folder could hide memories
            warnings.add(f"cannot read a folder: {exc.strerror}")

        top = self._root.joinpath(*self._subdir.split("/")) if self._subdir else self._root
        fsroot = longpath.fs(self._root)  # every path below is in the form the operating system takes, so none is too long for it
        count = 0
        stop = False
        for dirpath, dirnames, filenames in os.walk(longpath.fs(top), topdown=True, followlinks=False, onerror=walk_error):
            kept = []
            for d in sorted(dirnames):
                if d == ".git":  # the real git folder; look-alikes are reported below
                    continue
                if _bad_component(d):
                    state["complete"] = False
                    warnings.add(f"folder with an unsafe name skipped: {safe_text(d, 60)}")
                    continue
                try:
                    folder_info = os.lstat(os.path.join(dirpath, d))
                except OSError as exc:
                    state["complete"] = False
                    warnings.add(f"cannot inspect a folder: {exc.strerror}")
                    continue
                if stat.S_ISLNK(folder_info.st_mode) or _is_reparse_point(folder_info):
                    state["complete"] = False
                    warnings.add(f"folder {safe_text(d, 60)} is a link or junction; not followed")
                    continue
                kept.append(d)
            dirnames[:] = kept
            depth = len(Path(dirpath).relative_to(fsroot).parts)
            if depth >= MAX_WALK_DEPTH:
                if dirnames:
                    state["complete"] = False
                    warnings.add(f"folders deeper than {MAX_WALK_DEPTH} levels were not read")
                dirnames[:] = []
            for name in sorted(filenames):
                if not name.lower().endswith(self._suffixes):
                    continue
                full = os.path.join(dirpath, name)
                relative = Path(full).relative_to(fsroot).as_posix()
                relpath = _valid_relpath(relative, self._suffixes)
                if relpath is None:
                    state["complete"] = False
                    warnings.add(f"path with unsafe characters skipped: {safe_text(relative, 60)}")
                    continue
                count += 1
                if count > self._max_files:
                    state["complete"] = False
                    warnings.add(f"more than {self._max_files} memory files; the rest were not read")
                    stop = True
                    break
                text = self._read_working_file(full, relpath, warnings)
                if text is None:
                    state["complete"] = False
                    continue
                memories.append(Memory(id=relpath, text=text, scope={"store": self._store}))
            if stop:
                break
        return LiveMemories(memories=memories, complete=state["complete"], warnings=warnings.as_list())

    @staticmethod
    def _read_working_file(full: str, relpath: str, warnings: Warnings) -> str | None:
        label = safe_text(relpath, 60)
        try:
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                warnings.add(f"{label} is a symlink; not followed")
                return None
            if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
                warnings.add(f"{label} is not a plain regular file; skipped")
                return None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(full, flags)
        except OSError as exc:
            warnings.add(f"{label} could not be opened: {exc.strerror}")
            return None
        try:
            fstat = os.fstat(fd)
            if not stat.S_ISREG(fstat.st_mode):
                warnings.add(f"{label} is not a regular file; skipped")
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            warnings.add(f"{label} could not be read: {exc.strerror}")
            return None
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES:
            return _too_large(fstat.st_size)
        return _text_from_bytes(data)

    def history(self, memory_id: str) -> list[MemoryEvent]:
        """Returns the events for one file, filtered from the global history.

        Reads at most the oldest 100,000 changes of the repository, so events beyond that are not returned and no
        warning says so.

        Raises:
            AdapterError: If `memory_id` is not a usable id, or git fails or times out.
        """
        if clean_id(memory_id) is None:
            raise AdapterError(f"memory_id must be a non-empty string of at most {MAX_ID_CHARS} characters")
        return [e for e in self.read_history(100_000).events if e.memory_id == memory_id]
