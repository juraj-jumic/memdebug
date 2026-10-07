"""Where the ledger lives by default: a per-user folder, not the current directory (which could be
inside the repository being watched, or a shared folder)."""
from __future__ import annotations

import os
from pathlib import Path


def default_ledger_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "memdebug" / "ledger.db"


def backup_root_for(ledger_path: Path) -> Path:
    """Where a plain-folder rollback keeps what it replaced: next to the ledger it is recorded in, never inside the notes."""
    return ledger_path.with_name("backups")


def default_config_path() -> Path:
    """The list of stores memdebug has been told to watch, next to the default ledger."""
    return default_ledger_path().with_name("stores.json")
