"""Where the ledger lives by default: a per-user folder, not the current directory.

The current directory could be inside the repository being watched, or a shared folder. This module holds the default locations of
the ledger, the list of watched stores and the rollback backups.
"""
from __future__ import annotations

import os
from pathlib import Path


def default_ledger_path() -> Path:
    """The default ledger file, `memdebug/ledger.db` in the user's data folder.

    This is `%LOCALAPPDATA%` on Windows and `$XDG_DATA_HOME` (or `~/.local/share`) elsewhere. Nothing is created.
    """
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
