"""Which agent session wrote a note? Evidence from Claude Code's own session logs, never proof.

Claude Code keeps every conversation as a log (`~/.claude/projects/<project>/<session>.jsonl`). When it edits a file with its Write or Edit tool,
the log records the call with the file's path and a time. Given a note that changed and the period it changed in, this finds the calls that
wrote that path in that period.

What it is not:
  * proof. Anyone who can edit a memory folder can also edit or delete a log, and a deleted log looks the same as a note nobody wrote. "No log
    explains this" means nothing by itself;
  * complete. A change made by a shell command (`echo >> note.md`) has no file path in the log, so it cannot be matched. The commands that ran in the
    period are counted, never read, so a person can tell "nothing wrote it" from "something might have".

The logs hold whole conversations, and their text is hostile like any other. Nothing is returned except a session id, a record id and a time,
each checked against a strict pattern, and the name of one of a fixed list of tools. No path and no text from a log ever leaves this module.
Logs are only read, never followed through a link, and read within fixed limits; when a limit stops the search, the result says it is incomplete.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import longpath
from .adapters.markdown_git import _is_reparse_point, _valid_relpath

MAX_PROJECT_FOLDERS = 1000
MAX_LOG_FILES = 500
MAX_LOG_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_WRITERS = 20
CLOCK_SLACK = timedelta(seconds=5)

WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}  # tool -> input field holding the path
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_TIME = re.compile(r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z\Z")
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class Writer:
    """One logged call that wrote the path. Nothing but ids, a tool name from a fixed list, and a time."""
    session: str
    record: str
    tool: str
    at: datetime


@dataclass(frozen=True)
class Provenance:
    writers: tuple[Writer, ...]   # newest first, at most MAX_WRITERS
    shell_calls: int              # shell commands that ran in the period; they might have written the file and cannot be matched
    logs_read: int
    complete: bool                # False when a limit, an unreadable log or an oversized line meant some of the period was not searched


@dataclass(frozen=True)
class Explanation:
    """What a search for one change's writer came to: either why nothing was searched, or the result."""
    reason: str | None = None     # set when no search was made
    result: Provenance | None = None


def note_paths(root: str | os.PathLike[str], memory_id: str) -> list[str]:
    """Where a note lives, spelled the ways a log might spell it (as registered, and with links resolved). The id comes from the ledger, which
    anyone could edit, so a path is built only from an id that passes the check the store readers apply to every file name; else nothing."""
    if _valid_relpath(memory_id, (".md",)) is None:
        return []
    parts = memory_id.split("/")
    spellings = {os.path.join(os.fspath(root), *parts)}
    try:
        spellings.add(os.path.join(os.path.realpath(root), *parts))
    except (OSError, ValueError):
        pass
    return sorted(spellings)


def explain_change(root: str | os.PathLike[str], memory_id: str, since: datetime | None, noticed: datetime, home: Path | None = None) -> Explanation:
    """Search the session logs for what wrote one note between `since` (the last time the ledger recorded anything about it) and `noticed`.
    `since` is a looser bound than the true last look, so the real write is never left out; it may take in earlier ones."""
    if since is None:
        return Explanation(reason="no earlier record of this store to say when the change happened")
    if since.tzinfo is None or noticed.tzinfo is None:
        return Explanation(reason="the time of the change is not known")
    paths = note_paths(root, memory_id)
    if not paths:
        return Explanation(reason="the note's name is not one memdebug can look up")
    return Explanation(result=find_writers(paths, since, noticed + CLOCK_SLACK, home))


def describe_explanation(explanation: Explanation) -> str:
    """One line for a person. It states what was found and what it cannot show; every value in it was checked or comes from a fixed list."""
    if explanation.result is None:
        return f"not searched: {explanation.reason}"
    found = explanation.result
    incomplete = "; part of the period could not be searched" if not found.complete else ""
    if found.writers:
        latest = found.writers[0]
        more = f" (and {len(found.writers) - 1} more)" if len(found.writers) > 1 else ""
        return (f"a Claude Code session logged a call to its {latest.tool} tool on it at {latest.at:%Y-%m-%d %H:%M:%S} UTC, "
                f"session {latest.session[:8]}{more}{incomplete}")
    if found.logs_read == 0:
        return "no Claude Code session logs were found to check" + incomplete
    shell = (f"; {found.shell_calls} shell command{'' if found.shell_calls == 1 else 's'} ran in that time and cannot be matched"
             if found.shell_calls else "")
    return f"no logged edit explains it{shell}{incomplete}. A deleted log looks the same, so this is not proof of anything"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_plain(info: os.stat_result, want_dir: bool) -> bool:
    kind = stat.S_ISDIR(info.st_mode) if want_dir else stat.S_ISREG(info.st_mode)
    return kind and not stat.S_ISLNK(info.st_mode) and not _is_reparse_point(info)


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not _TIME.match(value):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def _log_files(root: Path, start: datetime) -> tuple[list[tuple[str, int]], bool]:
    """The session logs that could hold a call made since `start`: plain files directly inside real project folders. A log last written
    before `start` cannot, so it is skipped without being opened."""
    try:
        if not _is_plain(longpath.lstat(root), True):
            return [], True
        folders = sorted(os.scandir(longpath.fs(root)), key=lambda e: e.name)  # names come back plain; the paths built from them are in the form the system takes
    except OSError:
        return [], True
    complete = len(folders) <= MAX_PROJECT_FOLDERS
    found: list[tuple[float, str, int]] = []
    for folder in folders[:MAX_PROJECT_FOLDERS]:
        try:
            if not _is_plain(folder.stat(follow_symlinks=False), True):
                continue
            entries = list(os.scandir(folder.path))
        except OSError:
            complete = False
            continue
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                complete = False
                continue
            if not _is_plain(info, False):
                continue
            try:
                if datetime.fromtimestamp(info.st_mtime, timezone.utc) + CLOCK_SLACK < start:
                    continue
            except (OverflowError, OSError, ValueError):  # a time that cannot be represented: search the log rather than guess
                pass
            found.append((info.st_mtime, entry.path, info.st_size))
    found.sort(reverse=True)
    if len(found) > MAX_LOG_FILES:
        complete = False
    return [(path, size) for _, path, size in found[:MAX_LOG_FILES]], complete


def _lines(path: str):
    """Lines of one log as bytes, read without following a link. Yields None for a line too long to read, then carries on after it."""
    fd = os.open(path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while True:
                line = handle.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(MAX_LINE_BYTES)
                    yield None
                    continue
                yield line
    finally:
        os.close(fd)


def _calls(record: object):
    """The (session, record id, time, tool, input) of every tool call in one log record, with the ids checked."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return
    session, uuid, at = record.get("sessionId"), record.get("uuid"), _when(record.get("timestamp"))
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if at is None or not isinstance(content, list) or not (isinstance(session, str) and _UUID.match(session)) \
            or not (isinstance(uuid, str) and _UUID.match(uuid)):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("name"), str):
            yield session, uuid, at, block["name"], block.get("input")


def find_writers(path: str | os.PathLike[str] | Sequence[str], start: datetime, end: datetime, home: Path | None = None) -> Provenance:
    """The logged calls that wrote `path` between `start` and `end` (both timezone-aware). `path` may be several spellings of one file. Paths
    are compared as text and never opened."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must carry a time zone")
    spellings = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    targets = {_norm(os.fspath(p)) for p in spellings}
    root = (home or Path.home()) / ".claude" / "projects"
    files, complete = _log_files(root, start)
    writers: dict[tuple[str, str, str], Writer] = {}
    shell_calls = logs_read = budget = 0
    for log, size in files:
        if size > MAX_LOG_BYTES or budget + size > MAX_TOTAL_BYTES:
            complete = False
            continue
        budget += size
        try:
            for line in _lines(log):
                if line is None:
                    complete = False
                    continue
                if b'"tool_use"' not in line:  # cheap test first: most lines are not tool calls
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                for session, uuid, at, tool, given in _calls(record):
                    if not start <= at <= end:
                        continue
                    if tool in SHELL_TOOLS:
                        shell_calls += 1
                    elif tool in WRITE_TOOLS and isinstance(given, dict):
                        written = given.get(WRITE_TOOLS[tool])
                        if isinstance(written, str) and _norm(written) in targets:
                            writers[(session, uuid, tool)] = Writer(session, uuid, tool, at)
            logs_read += 1
        except OSError:
            complete = False
    newest = sorted(writers.values(), key=lambda w: w.at, reverse=True)
    return Provenance(tuple(newest[:MAX_WRITERS]), shell_calls, logs_read, complete and len(newest) <= MAX_WRITERS)
