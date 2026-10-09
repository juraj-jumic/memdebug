"""Restore a plain folder of markdown notes (no git) to a snapshot.

A folder keeps no history, so the only record of what a file used to hold is the snapshot memdebug took of it. That changes
three things compared with the git engine (restore.py); everything else is the same code or the same rules:

  * NO EXACT BYTES. A snapshot holds normalised text (line endings changed, undecodable bytes replaced, long text cut), and
    there is no git blob to take the original from. Files are rebuilt from the snapshot's text: UTF-8, with LF line endings,
    or CRLF if the file being replaced uses CRLF throughout. Text that is cut, marked too large, or had undecodable bytes is
    refused, never written back.
  * BACKUPS ARE FILES. Anything a rollback would overwrite or delete is first copied, byte for byte, into a private folder
    next to the ledger, and the copy is read back and checked before the notes folder is touched.
  * NO COMMIT. Files are changed in place. The way back is the snapshot memdebug takes just before (see rollback_flow.py).

Shared with the git engine (fileops.py): links, junctions and anything that is not a plain file are never followed; names that
differ only by letter case are refused; files are written through a temporary file and an atomic rename; every write is
journaled and a failure undoes the whole rollback. PLANNING WRITES NOTHING, and applying re-plans from scratch and refuses if
anything differs from the plan the person confirmed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .. import longpath
from ..errors import RestoreError
from ..models import Snapshot
from ..textsafe import safe_text
from .fileops import _O_BINARY, _O_NOFOLLOW, FileOps
from .folder import FolderAdapter
from .markdown_git import MAX_FILE_BYTES, _text_from_bytes, _valid_relpath
from .restore import MAX_ITEMS, MAX_TOTAL_BYTES, Item, Outcome, Plan, _plan_id, snapshot_text_problem

_NOT_A_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _crlf_throughout(data: bytes | None) -> bool:
    """True when every line break in the file is a CRLF, so rebuilding it with CRLF puts back what a snapshot cannot hold."""
    return data is not None and b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n")


class FolderRestorer(FileOps):
    """The rollback engine for a plain-folder store (see the module docstring for its rules).

    Use `plan` to see what a rollback would do, and `apply` to do it. Only `apply` writes: to the notes folder and to
    the backup folder under `backup_root`. The backup folder must not lie inside the notes folder or contain it; if it
    does, the plan has a blocker and nothing is applied.

    Args:
        adapter: The folder adapter for the notes being restored.
        backup_root: The folder (next to the ledger) under which backups are created, one folder per rollback.
    """

    def __init__(self, adapter: FolderAdapter, backup_root: str | os.PathLike[str]):
        self._adapter = adapter
        self._root = adapter.root
        self._backup_root = Path(backup_root)

    # -- planning (writes nothing) ----------------------------------------------------------------------------------

    def _backup_problem(self) -> str | None:
        """Memdebug's backups must never sit inside the notes (they would be read as memories) or contain them."""
        notes = os.path.normcase(longpath.realpath(self._root))
        backups = os.path.normcase(longpath.realpath(self._backup_root))
        if backups == notes or backups.startswith(notes + os.sep):
            return "memdebug's backup folder is inside the folder being restored; give the ledger a location outside it (--db)"
        if notes.startswith(backups + os.sep):
            return "the folder being restored is inside memdebug's backup folder; give the ledger a location outside it (--db)"
        return None

    @staticmethod
    def _bytes_for(text: str, live_bytes: bytes | None) -> tuple[bytes, str]:
        if _crlf_throughout(live_bytes):
            return (text.replace("\n", "\r\n").encode("utf-8"),
                    "rebuilt from the snapshot's text; line endings kept as CRLF, like the file it replaces")
        return text.encode("utf-8"), "rebuilt from the snapshot's text (a snapshot does not keep line endings, so they are LF)"

    def plan(self, snapshot: Snapshot, *, only: Sequence[str] = (), remove_added: bool = False) -> Plan:
        """Compares the snapshot with the folder and reports what a rollback would do. Writes nothing.

        Files that cannot be safely written (links, folders, over-large files, unsafe names, files this store does
        not cover, text the snapshot cannot hold faithfully) are listed in `skipped`.
        Unsafe states (a backup folder inside the notes or the reverse, names that differ only by letter case, too many
        files) are reported as blockers on the returned plan.

        Args:
            snapshot: The snapshot to restore.
            only: Restrict the plan to these files. Empty means every file in the snapshot.
            remove_added: Also plan to remove files added since the snapshot. This is done only when both the snapshot and
                the current listing are complete; otherwise a warning is given and nothing is removed.

        Raises:
            RestoreError: If the snapshot is from a different store, a name in `only` is not a usable memory file name, or
                a name in `only` is in neither the snapshot nor the store.
        """
        info = snapshot.info
        adapter = self._adapter
        if info.backend != adapter.name or info.scope != {"store": adapter.store}:
            raise RestoreError("that snapshot was taken from a different memory store")
        plan = Plan(snapshot_id=info.id, store=adapter.store)
        problem = self._backup_problem()
        if problem is not None:
            plan.blockers.append(problem)
        by_path = {m.id: m for m in snapshot.memories}
        chosen: set[str] | None = None
        if only:
            chosen = set()
            for name in only:
                rel = _valid_relpath(name, adapter.suffixes)
                if rel is None:
                    raise RestoreError(f"not a usable memory file name: {safe_text(name, 60)}")
                chosen.add(rel)

        live_ids: set[str] = set()
        live_complete = False
        if remove_added:
            if not info.complete:
                plan.warnings.append("the snapshot may be incomplete, so files added since cannot be told apart; none will be removed")
            else:
                live = adapter.list_memories({"store": adapter.store})
                live_complete = live.complete
                live_ids = {m.id for m in live.memories}
                if not live.complete:
                    plan.warnings.append("some folders could not be read, so files added since cannot be listed; none will be removed")
        if chosen is not None:
            unknown = sorted(c for c in chosen if c not in by_path and c not in live_ids)
            if unknown:
                raise RestoreError("not in the snapshot (nor in the store): " + ", ".join(safe_text(u, 60) for u in unknown[:5]))

        total = 0
        for path in sorted(by_path):
            memory = by_path[path]
            rel = _valid_relpath(path, adapter.suffixes)
            if rel is None:
                plan.skipped.append((safe_text(path, 60), "the file name is not safe to write"))
                continue
            if chosen is not None and rel not in chosen:
                continue
            if adapter.subdir and not rel.startswith(adapter.subdir + "/"):
                plan.skipped.append((rel, "outside the folder this store covers"))
                continue
            if adapter.only is not None and rel not in adapter.only:
                plan.skipped.append((rel, "not one of the files this store watches"))
                continue
            if memory.scope != {"store": adapter.store}:
                plan.skipped.append((rel, "belongs to another store"))
                continue
            status, live_bytes = self._inspect(rel)
            if status == "unsafe":
                plan.skipped.append((rel, "exists but is a link, a folder or not a plain file; never written"))
                continue
            if status == "large":
                plan.skipped.append((rel, "the file in the store is too large to handle"))
                continue
            live_text = None if live_bytes is None else _text_from_bytes(live_bytes)
            if live_text == memory.text:
                plan.unchanged += 1
                continue
            reason = snapshot_text_problem(memory.text)
            if reason is not None:
                plan.skipped.append((rel, f"the snapshot cannot put it back faithfully: {reason}"))
                continue
            data, note = self._bytes_for(memory.text, live_bytes)  # a Memory's text is always valid, so it always encodes
            if len(data) > MAX_FILE_BYTES:
                plan.skipped.append((rel, "the restored file would be too large to handle"))
                continue
            saved = live_bytes is not None
            item = Item(rel, "restore" if saved else "recreate", "snapshot", live_bytes, live_text, data, memory.text, None, None,
                        None, commit=False, backup=saved, note=note + ("; the file it replaces is saved first" if saved else ""))
            total += len(data) + len(live_bytes or b"")
            plan.items.append(item)

        if remove_added and live_complete:
            for rel in sorted(live_ids - set(by_path)):
                if chosen is not None and rel not in chosen:
                    continue
                status, live_bytes = self._inspect(rel)
                if status != "ok":
                    plan.skipped.append((rel, "added since, but cannot be safely read, so it is left alone"))
                    continue
                total += len(live_bytes or b"")
                plan.items.append(Item(rel, "remove", "none", live_bytes, None if live_bytes is None else _text_from_bytes(live_bytes),
                                       None, None, None, None, None, commit=False, backup=True,
                                       note="added since the snapshot; saved first"))
        elif not remove_added:
            live = adapter.list_memories({"store": adapter.store})
            plan.kept_new = sorted(m.id for m in live.memories if m.id not in by_path and (chosen is None or m.id in chosen))

        if len(plan.items) > MAX_ITEMS or total > MAX_TOTAL_BYTES:
            plan.blockers.append("this rollback touches too many or too large files to do in one step; use --only")
        folded: dict[str, str] = {}
        for item in plan.items:
            other = folded.setdefault(item.path.casefold(), item.path)
            if other != item.path:
                plan.blockers.append(f"{safe_text(item.path, 60)} and {safe_text(other, 60)} differ only by letter case")
        plan.items.sort(key=lambda i: i.path)
        plan.plan_id = _plan_id(plan)
        return plan

    # -- applying -----------------------------------------------------------------------------------------------------

    def apply(self, snapshot: Snapshot, *, expected_plan_id: str, only: Sequence[str] = (), remove_added: bool = False,
              now: datetime | None = None) -> Outcome:
        """Carry out the plan the person confirmed. Re-plans first and refuses if anything has changed since.

        This is the only method that writes to the notes. It first copies everything it would overwrite or delete into
        a new backup folder (and checks the copies), then changes the files in place. If any step fails, the files are
        put back (the backup folder is kept). There is no commit, so the outcome's `branch`, `previous_head` and `commit`
        are None.

        Args:
            snapshot: The snapshot to restore.
            expected_plan_id: The `plan_id` of the plan the person confirmed.
            only: The same restriction that was given to `plan`.
            remove_added: The same setting that was given to `plan`.
            now: The time to record in the backup name; defaults to the current time.

        Returns:
            What was done, including where the backup is. If the plan has nothing to change, nothing is written and no
            backup is made.

        Raises:
            RestoreError: If the new plan has blockers, differs from the confirmed plan, the backup could not be
                written (nothing was changed), or a step fails. The message says whether the rollback was undone; if it
                could not be fully undone, it lists what remains and names the backup folder.
        """
        now = now or datetime.now(timezone.utc)
        plan = self.plan(snapshot, only=only, remove_added=remove_added)
        if plan.blockers:
            raise RestoreError("cannot roll back right now: " + "; ".join(plan.blockers) + ". Nothing was changed.")
        if plan.plan_id != expected_plan_id:
            raise RestoreError("the folder changed after the plan was made. Nothing was changed; run it again.")
        if not plan.items:
            return Outcome(plan.snapshot_id, None, None, None, None, [])

        for item in plan.items:  # every write is checked before the first one happens
            if item.action != "remove":
                self._check_target(item.path)
            elif item.live_bytes is not None:
                self._check_target(item.path, must_exist=True)

        saved = [i for i in plan.items if i.backup]
        backup_ref, backup_path = self._save_backup(saved, plan, now) if saved else (None, None)

        journal: list[tuple[str, bytes | None]] = []
        created_dirs: list[str] = []
        try:
            for item in plan.items:
                if item.action == "remove":
                    self._remove_file(item.path)
                else:
                    created_dirs += self._write_file(item.path, item.target_bytes or b"")
                journal.append((item.path, item.live_bytes))
            self._verify_files(plan.items)
        except BaseException as failure:
            problems = self._undo_files(journal, created_dirs)
            where = f" Your previous content is saved in {backup_path}." if backup_path else ""
            if problems:
                raise RestoreError(f"the rollback failed ({safe_text(failure, 160)}) and could not be fully undone "
                                   f"({'; '.join(problems)}).{where}") from failure
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise
            raise RestoreError(f"the rollback failed ({safe_text(failure, 200)}) and was undone; nothing was changed.") from failure
        return Outcome(plan.snapshot_id, None, None, None, backup_ref, plan.items, None if backup_path is None else str(backup_path))

    # -- backups ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _private_dirs(path: Path) -> None:
        """Create a folder and any missing parents, readable by this user only (on systems that have such modes)."""
        missing: list[Path] = []
        current = path
        while not longpath.exists(current):
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
        for folder in reversed(missing):
            os.mkdir(longpath.fs(folder), 0o700)
            if os.name == "posix":
                os.chmod(folder, 0o700)

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        fd = os.open(longpath.fs(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _save_backup(self, items: list[Item], plan: Plan, now: datetime) -> tuple[str, Path]:
        """Copies what is about to be overwritten or deleted into a new private backup folder, and checks the copies.

        The copies are byte for byte, with a manifest listing them. The name is recorded in the ledger in the same form
        the git engine uses; here it names a folder, not a git ref. If anything fails, the new backup folder is deleted
        and the error is raised before the notes are touched.

        Returns:
            The backup's name (in the git engine's ref form) and the folder it was written to.

        Raises:
            RestoreError: If the backup folder overlaps the notes folder, or a copy cannot be written or does not match.
        """
        problem = self._backup_problem()
        if problem is not None:
            raise RestoreError(problem + ". Nothing was changed.")
        stamp = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        ref = f"refs/memdebug/backups/{stamp}"
        target = self._backup_root / (_NOT_A_NAME.sub("_", plan.store)[:60] or "store") / stamp
        files: list[dict] = []
        try:
            self._private_dirs(target / "files")
            for item in items:
                data = item.live_bytes or b""
                destination = target / "files" / Path(*item.path.split("/"))
                self._private_dirs(destination.parent)
                self._write_private(destination, data)
                digest = hashlib.sha256(data).hexdigest()
                if hashlib.sha256(Path(longpath.fs(destination)).read_bytes()).hexdigest() != digest:
                    raise RestoreError(f"the copy of {safe_text(item.path, 60)} did not match the original")
                files.append({"path": item.path, "action": item.action, "bytes": len(data), "sha256": digest})
            manifest = {"format": 1, "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "store": plan.store, "snapshot": plan.snapshot_id,
                        "plan": plan.plan_id, "files": files}
            self._write_private(target / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
        except BaseException as failure:
            shutil.rmtree(longpath.fs(target), ignore_errors=True)
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise
            raise RestoreError(f"the backup could not be written ({safe_text(failure, 120)}), so nothing was changed.") from failure
        return ref, target
