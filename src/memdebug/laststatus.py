"""The record of the latest check: written by `check` and `watch`, read by the viewer.

The viewer shows only what is in the ledger, so it could not say that a store was not read in full, or when the last look was. Instead of letting it
open the live stores (which would need Docker, adapters and the registry), `check` leaves a small `status.json` next to the ledger and the
viewer reads that. The file is a convenience, not evidence: it is not chained into the ledger, anyone who can write the folder can change it, and
the viewer says when it was written. Everything read back is treated as untrusted text and cleaned again.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import MemdebugError
from .textsafe import safe_text

VERSION = 1
FILE_NAME = "status.json"
MAX_BYTES = 200_000
MAX_STORES = 100
MAX_LINES = 12
STATES = ("quiet", "changes", "attention", "incomplete", "error")  # quiet and changes are fine; the others need a look


class StatusError(MemdebugError):
    """The record of the latest check exists but cannot be used."""


@dataclass(frozen=True)
class StoredStore:
    """One store's line in the latest check.

    Attributes:
        name: The store's name.
        kind: Its kind (folder, markdown, openwebui, mem0).
        state: One of STATES.
        headline: The one-line summary `memdebug check` printed for it.
        lines: The detail lines printed under it (wording worth a second look, who wrote it, where it came from).
        memories: How many memories it held at the time, or None if that was not known.
    """

    name: str
    kind: str
    state: str
    headline: str
    lines: tuple[str, ...]
    memories: int | None


@dataclass(frozen=True)
class StoredCheck:
    """The latest check as the viewer shows it.

    Attributes:
        checked_at: When the check finished (UTC).
        ledger_ok: Whether the ledger verified at that time.
        ledger_problem: The first problem if it did not.
        stores: One entry per watched store.
        note: What happened to the witness file, if one is configured.
    """

    checked_at: datetime
    ledger_ok: bool
    ledger_problem: str | None
    stores: tuple[StoredStore, ...]
    note: str | None

    @property
    def needs_a_look(self) -> bool:
        """Whether any store was not read in full, could not be read, or has a change outside its history, or the ledger failed."""
        return not self.ledger_ok or any(s.state in ("attention", "incomplete", "error") for s in self.stores)


def status_path(ledger_path: str | Path) -> Path:
    """Where the latest check is kept for a ledger: next to it."""
    return Path(ledger_path).with_name(FILE_NAME)


def write(path: Path, record: dict) -> bool:
    """Save a record atomically. Returns False (and writes nothing) if the target is a link or the folder cannot be written; never raises."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return False
        except FileNotFoundError:
            pass
        text = json.dumps(record, ensure_ascii=True, sort_keys=True, indent=1)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            handle.write(text)
        os.replace(temp, path)
        return True
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False


def _text(value: object, limit: int) -> str:
    return safe_text(value, limit) if isinstance(value, str) else ""


def read(path: Path) -> StoredCheck | None:
    """The latest check, or None if none has been recorded.

    Raises:
        StatusError: If the file is not a regular file, is too large, or does not hold a record of the expected shape.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StatusError("the record of the latest check cannot be read") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise StatusError("the record of the latest check is not usable")
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise StatusError("the record of the latest check cannot be read") from exc
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("stores"), list):
        raise StatusError("the record of the latest check has an unexpected shape")
    try:
        checked_at = datetime.fromisoformat(str(data["checked_at"]))
    except (KeyError, ValueError) as exc:
        raise StatusError("the record of the latest check has no usable time") from exc
    if checked_at.tzinfo is None:
        raise StatusError("the record of the latest check has no usable time")
    stores = []
    for raw in data["stores"][:MAX_STORES]:
        if not isinstance(raw, dict):
            raise StatusError("the record of the latest check has an unexpected shape")
        state = raw.get("state")
        lines: list = raw["lines"] if isinstance(raw.get("lines"), list) else []
        count = raw.get("memories")
        stores.append(StoredStore(
            name=_text(raw.get("name"), 40), kind=_text(raw.get("kind"), 20), state=state if state in STATES else "error",
            headline=_text(raw.get("headline"), 400), lines=tuple(_text(line, 400) for line in lines[:MAX_LINES] if isinstance(line, str)),
            memories=count if isinstance(count, int) and not isinstance(count, bool) and 0 <= count < 10**9 else None))
    problem = data.get("ledger_problem")
    note = data.get("note")
    return StoredCheck(checked_at=checked_at.astimezone(timezone.utc), ledger_ok=data.get("ledger_ok") is True,
                       ledger_problem=_text(problem, 300) or None if isinstance(problem, str) else None,
                       stores=tuple(stores), note=_text(note, 300) or None if isinstance(note, str) else None)
