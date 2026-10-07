"""A witness: a second copy of the ledger's head hash, kept somewhere else.

The ledger's hash chain proves nothing was edited *inside* it, but whoever can rewrite the whole ledger file can build
a new valid chain, or cut off its newest entries, and `verify` will still say "intact". A witness closes that gap: each
time you run `memdebug witness --file PATH` one line is appended to a text file recording the ledger's newest entry and
its hash. Later, `memdebug verify --witness PATH` checks that the ledger still contains exactly that entry. A ledger that
was rewritten or truncated can no longer produce it.

The witness file chains its own lines (each line carries the hash of the line before), so editing or deleting a line
shows up as well. It holds hashes and counts only, never memory text.

It is only as strong as its separation from the ledger: put the file where an attacker who controls the ledger cannot
also rewrite it (another drive or machine, a synced folder, a USB stick, a repository you push to). A witness on the same
disk buys little, and memdebug says so.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .adapters.markdown_git import _is_reparse_point
from .errors import MemdebugError
from .ledger import Ledger
from .textsafe import safe_text

MAX_WITNESS_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 1024
GENESIS = "0" * 64
_HASH = re.compile(r"^[0-9a-f]{64}\Z")
_KEYS = {"v", "seq", "head", "count", "at", "prev"}


class WitnessError(MemdebugError):
    """The witness file cannot be used (not a plain file, corrupt, unreadable)."""


@dataclass(frozen=True)
class WitnessLine:
    seq: int
    head: str
    count: int
    at: str
    prev: str
    text: str  # the exact line, without its line break

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class WitnessCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: int = 0
    last_seq: int = 0
    unwitnessed: int = 0  # ledger entries newer than the last witnessed one


def _line_text(seq: int, head: str, count: int, at: str, prev: str) -> str:
    return json.dumps({"v": 1, "seq": seq, "head": head, "count": count, "at": at, "prev": prev},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _check_path(path: Path, *, must_exist: bool) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if must_exist:
            raise WitnessError("the witness file does not exist") from None
        if not path.parent.is_dir():
            raise WitnessError("the folder for the witness file does not exist") from None
        return
    except OSError as exc:
        raise WitnessError(f"cannot look at the witness file ({exc.strerror})") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise WitnessError("the witness file must be a plain file, not a link or a folder")


def read_witness(path: Path) -> tuple[list[WitnessLine], list[str]]:
    """The lines of a witness file and any problems with the file itself (chain, format, order)."""
    _check_path(path, must_exist=True)
    try:
        raw = path.read_bytes() if path.stat().st_size <= MAX_WITNESS_BYTES else None
    except OSError as exc:
        raise WitnessError(f"cannot read the witness file ({exc.strerror})") from exc
    if raw is None:
        raise WitnessError("the witness file is unexpectedly large")
    problems: list[str] = []
    lines: list[WitnessLine] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], ["the witness file is not valid text"]
    # Line-ending style and a leading byte-order mark carry no meaning (the chain hashes each line's text), and they change
    # when the file passes through git's autocrlf or an editor, so they must not look like tampering.
    text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    previous = GENESIS
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # the final line break
    for number, line in enumerate(parts, 1):
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            problems.append(f"witness line {number} is too long")
            break
        try:
            data = json.loads(line)
        except ValueError:
            problems.append(f"witness line {number} is not valid")
            break
        if (not isinstance(data, dict) or set(data) != _KEYS or data["v"] != 1 or not isinstance(data["seq"], int)
                or isinstance(data["seq"], bool) or data["seq"] < 1 or not isinstance(data["count"], int)
                or isinstance(data["count"], bool) or data["count"] < data["seq"] or not isinstance(data["head"], str)
                or not _HASH.match(data["head"]) or not isinstance(data["prev"], str) or not isinstance(data["at"], str)
                or not _HASH.match(data["prev"])):
            problems.append(f"witness line {number} has unexpected contents")
            break
        canonical = _line_text(data["seq"], data["head"], data["count"], data["at"], data["prev"])
        if canonical != line:
            problems.append(f"witness line {number} was not written by memdebug (its form differs)")
            break
        entry = WitnessLine(data["seq"], data["head"], data["count"], data["at"], data["prev"], line)
        if entry.prev != previous:
            problems.append(f"witness line {number} does not follow the line before it: a line was changed or removed")
        if lines and entry.seq < lines[-1].seq:
            problems.append(f"witness line {number} goes back in time")
        lines.append(entry)
        previous = entry.digest
    return lines, problems


def append_witness(ledger: Ledger, path: Path, *, now: datetime | None = None) -> tuple[WitnessLine, bool]:
    """Record the ledger's newest entry in the witness file. Returns the line and whether it was newly written."""
    _check_path(path, must_exist=False)
    existing: list[WitnessLine] = []
    if os.path.lexists(path):
        existing, problems = read_witness(path)
        if problems:
            raise WitnessError("the witness file is damaged (" + problems[0] + "), so it will not be extended")
    entries = ledger.entries()
    if not entries:
        raise WitnessError("the ledger is empty; there is nothing to witness yet")
    newest = entries[-1]
    if existing and existing[-1].seq == newest.seq and existing[-1].head == newest.hash:
        return existing[-1], False
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = existing[-1].digest if existing else GENESIS
    text = _line_text(newest.seq, newest.hash, len(entries), at, prev)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        os.write(fd, (text + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return WitnessLine(newest.seq, newest.hash, len(entries), at, prev, text), True


def same_disk(ledger_path: Path, witness_path: Path) -> bool:
    try:
        return os.stat(ledger_path).st_dev == os.stat(witness_path.parent).st_dev
    except OSError:
        return False


SAME_DISK_WARNING = ("the witness is on the same disk as the ledger, so it gives little protection if that disk is "
                     "compromised; keep it on another drive, machine or synced folder")


def verify_witness(ledger: Ledger, path: Path, *, ledger_path: Path | None = None) -> WitnessCheck:
    """Does the ledger still contain every entry the witness recorded?"""
    lines, problems = read_witness(path)
    check = WitnessCheck(ok=False, problems=list(problems), lines=len(lines))
    if not lines and not problems:
        check.problems.append("the witness file is empty")
    total = ledger.counts()["events"]
    for line in lines:
        entry = ledger.get_entry(f"e{line.seq}") if line.seq <= total else None
        if entry is None:
            check.problems.append(f"the witness recorded entry {line.seq}, but the ledger has only {total} entries: "
                                  "the newest entries were removed")
            break
        if entry.hash != line.head:
            check.problems.append(f"entry {line.seq} is not what the witness recorded on {safe_text(line.at, 25)}: "
                                  "the ledger was rewritten")
            break
    if lines:
        check.last_seq = lines[-1].seq
        check.unwitnessed = max(0, total - lines[-1].seq)
    if ledger_path is not None and same_disk(ledger_path, path):
        check.warnings.append(SAME_DISK_WARNING)
    check.ok = not check.problems
    return check
