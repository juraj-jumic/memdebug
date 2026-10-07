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
        if len(self._items) < self._cap:
            self._items.append(message)
        else:
            self._extra += 1

    def as_list(self) -> list[str]:
        items = list(self._items)
        if self._extra:
            items.append(f"... and {self._extra} more warnings")
        return items


def clean_id(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= MAX_ID_CHARS:
        return value
    return None


def parse_ts(value: object) -> datetime | None:
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
    try:
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(os.stat(resolved).st_mode):
            raise AdapterError("path is not a regular file")
    except OSError as exc:
        raise AdapterError(f"cannot access file: {exc.strerror}") from exc
    return resolved
