"""Small facts about store types that the viewer and the command line both need."""
from __future__ import annotations

import re

from .textsafe import safe_text

# Stores that keep no change history of their own: memdebug can only record what it sees between two looks, so it cannot say
# who made a change or whether it bypassed anything.
HISTORYLESS = frozenset({"folder", "openwebui"})

_OPAQUE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def is_opaque_id(memory_id: str) -> bool:
    """A random identifier (a UUID or a hash) that tells a person nothing, as opposed to a file name or a short id."""
    return _OPAQUE.fullmatch(memory_id) is not None


def short_id(memory_id: str) -> str:
    """An opaque id cut to its first 8 characters plus an ellipsis; any other id is left as it is."""
    return memory_id[:8] + "…" if is_opaque_id(memory_id) else memory_id


def friendly_id(memory_id: str, text: str | None, width: int = 40) -> str:
    """For an opaque id: its short form and the start of what the memory says. Otherwise the id itself, which is already meaningful."""
    if not is_opaque_id(memory_id):
        return safe_text(memory_id, 60)
    words = safe_text(" ".join((text or "").split()), width)
    return f'{memory_id[:8]}… "{words}"' if words else f"{memory_id[:8]}…"
