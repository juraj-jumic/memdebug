"""Small helpers shared by every adapter. Everything an adapter reads is untrusted."""
from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from ..errors import AdapterError
from ..models import MAX_ID_CHARS


def decode(raw: bytes) -> str:
    """Invalid UTF-8 in stored data must never crash a read."""
    return raw.decode("utf-8", "replace")


class Warnings:
    """Collects warnings but never grows without bound."""

    def __init__(self, cap: int = 50):
        self._items: list[str] = []
        self._cap = cap
        self._extra = 0

    def add(self, message: str) -> None:
        """Keeps the message, or only counts it once `cap` messages are already held."""
        if len(self._items) < self._cap:
            self._items.append(message)
        else:
            self._extra += 1

    def as_list(self) -> list[str]:
        """Returns the kept messages, plus a final line saying how many more were dropped, if any."""
        items = list(self._items)
        if self._extra:
            items.append(f"... and {self._extra} more warnings")
        return items


def clean_id(value: object) -> str | None:
    """Returns `value` if it is a non-empty string of at most MAX_ID_CHARS characters, else None."""
    if isinstance(value, str) and 0 < len(value) <= MAX_ID_CHARS:
        return value
    return None


def parse_ts(value: object) -> datetime | None:
    """Parses an ISO 8601 timestamp string into a UTC datetime.

    A trailing "Z" is accepted, and a timestamp without a zone is taken to be UTC.

    Returns:
        The time in UTC, or None if `value` is not a string, is empty or longer than 64 characters, or cannot be parsed.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)  # older rows carry no zone
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def require_regular_file(path: Path) -> Path:
    """Resolves `path` and checks that it is a regular file.

    Returns:
        The resolved path. Links are followed, so this is the real file.

    Raises:
        AdapterError: If the path does not exist, cannot be accessed, or is not a regular file.
    """
    try:
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(os.stat(resolved).st_mode):
            raise AdapterError("path is not a regular file")
    except OSError as exc:
        raise AdapterError(f"cannot access file: {exc.strerror}") from exc
    return resolved
