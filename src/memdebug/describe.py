"""One-line descriptions of events, shared by the command line and the viewer."""
from __future__ import annotations

import json

from .models import MemoryEvent


def rollback_details(event: MemoryEvent) -> dict | None:
    """What a rollback record says, or None if it is not readable (the viewer then shows only the id)."""
    from .ledger import validate_rollback_details

    try:
        return validate_rollback_details(json.loads(event.after or "{}"))
    except (ValueError, TypeError):
        return None


def describe_event(event: MemoryEvent) -> str:
    if event.op.value == "SNAPSHOT":
        try:
            info = json.loads(event.after or "{}")
            return f"{event.memory_id.removeprefix('snapshot:')}: {int(info['count'])} memories" + (
                "" if info.get("complete") else " (listing may be incomplete)")
        except (ValueError, KeyError, TypeError):
            return event.memory_id
    if event.op.value == "ROLLBACK":
        details = rollback_details(event)
        if details is None:
            return event.memory_id
        return f"restored to {details['target']}: {details['file_count']} file(s)"
    if event.op.value == "SNAP_DEL":
        return f"{event.memory_id.removeprefix('snapshot:')} deleted"
    return (event.after if event.after is not None else event.before) or ""
