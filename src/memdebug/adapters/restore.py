"""Restore a markdown/git memory store to a snapshot.

This is the only code in memdebug that changes a memory store, so it is built to fail safe:

  * PLANNING WRITES NOTHING. A plan is a read-only comparison of the snapshot with the repository. Applying
    re-plans from scratch and refuses if anything differs from the plan the person confirmed.
  * EXACT BYTES, NOT SNAPSHOT TEXT. A snapshot holds normalised text (line endings changed, undecodable bytes
    replaced, long text cut). Files are restored from the original git blob whose text matches the snapshot.
    Snapshot text is only a fallback, and is refused when it is cut, marked too large, or had undecodable bytes.
  * NOTHING IS LOST. Anything a rollback would overwrite or delete that git does not already hold (an uncommitted
    edit, an untracked file) is first saved under refs/memdebug/backups/ as a commit, so it can be recovered.
  * HISTORY IS NEVER REWRITTEN. Changes that must be committed become one new commit on top of the branch. It is
    built with git plumbing and a private index file, so no hook, filter, textconv or attribute from the
    (untrusted) repository can run, and the branch moves only if nobody committed in the meantime.
  * IT REFUSES unsafe states: a detached HEAD, staged changes, a merge or rebase in progress, git lock files,
    and any path that is a link, junction, device name, or differs from another path only by case.
  * IT UNDOES ITSELF. Every file written is journaled; if any step fails, files, branch and index are put back.

Files are written through a temporary file in the same folder and an atomic rename, never through a link.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ..errors import RestoreError
from ..models import Snapshot
from ..textsafe import safe_text
from .fileops import _O_BINARY, _O_NOFOLLOW, FileOps, _is_link  # noqa: F401  (re-exported: other code and tests use these names)
from .markdown_git import (
    _RAW_RE,
    _SHA_RE,
    MarkdownGitAdapter,
    _BlobReader,
    _text_from_bytes,
    _valid_relpath,
)

MAX_ITEMS = 5_000
MAX_TOTAL_BYTES = 200_000_000
MAX_HISTORY_PER_FILE = 200
HISTORY_COMMITS = 3000   # commits looked through, per batch of files, for the version a snapshot held
BATCH = 100              # files per git command, so command lines stay short on every system
COMMIT_NAME = "memdebug"
COMMIT_EMAIL = "memdebug@localhost"
_BRANCH_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/\-]{0,200}\Z")
_CUT_MARKER = re.compile(r"\.\.\.\[cut: \d+ chars, sha256 [0-9a-f]{16}\]\Z")
_TOO_LARGE = "[file too large to read:"
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply",
                "sequencer", "BISECT_LOG", "index.lock", "HEAD.lock", "shallow.lock")
_REGULAR = ("100644", "100755")


@dataclass
class Item:
    """One file a rollback would change, with what is on disk now and what it would hold afterwards.

    Used by both rollback engines. All paths are relative to the store root, with "/" separators.

    Attributes:
        path: The file, relative to the store root.
        action: "restore" (change a file back), "recreate" (put back a deleted file) or "remove" (delete a file added
            since the snapshot).
        source: Where the new content comes from: "git" (the original bytes), "snapshot" (rebuilt from the snapshot's
            text) or "none" (a removal).
        live_bytes: What the file holds now, or None if it does not exist.
        live_text: `live_bytes` as normalised text (line endings changed, undecodable bytes replaced).
        target_bytes: What the file would hold afterwards; None for a removal.
        target_text: The snapshot's text for the file; None for a removal.
        target_sha: The git blob the bytes came from, if any.
        head_mode: The file's mode in HEAD; None when git does not track it.
        head_bytes: The file's committed content, or None when git does not track it.
        commit: Whether the change is part of the new commit (always False for plain folders).
        backup: Whether the live content is saved first because git does not already hold it.
        note: A short human-readable explanation of how the new content was obtained.
        head_sha: The committed blob, so that a failed apply can put the index back.
    """

    path: str
    action: str              # restore: change a file back; recreate: put back a deleted file; remove: delete a file added since
    source: str              # "git": the original bytes; "snapshot": rebuilt from the snapshot's text; "none": a removal
    live_bytes: bytes | None
    live_text: str | None
    target_bytes: bytes | None
    target_text: str | None
    target_sha: str | None   # the git blob the bytes came from, if any
    head_mode: str | None    # mode in HEAD; None when git does not track the file
    head_bytes: bytes | None
    commit: bool             # part of the new commit
    backup: bool             # live content is not in git, so it is saved first
    note: str = ""
    head_sha: str | None = None  # the committed blob, so a failed apply can put the index back


@dataclass
class Plan:
    """What a rollback to one snapshot would do, worked out without changing anything.

    Used by both rollback engines. A plan with `blockers` cannot be applied.

    Attributes:
        snapshot_id: The snapshot being restored.
        store: The store the plan is for.
        branch: The branch a git rollback would commit to, if it is usable (git stores only).
        head: The commit that branch was at when the plan was made (git stores only).
        items: The files that would change, sorted by path.
        unchanged: How many snapshot files already match the store.
        kept_new: Files added since the snapshot that the rollback leaves alone.
        skipped: A (path, reason) pair for each file the rollback will not touch.
        warnings: Notes that do not stop the rollback.
        blockers: Reasons the rollback cannot be applied now.
        plan_id: A digest of what the plan would do. `apply` refuses to run if a fresh plan has a different one.
    """

    snapshot_id: str
    store: str
    branch: str | None = None
    head: str | None = None
    items: list[Item] = field(default_factory=list)
    unchanged: int = 0
    kept_new: list[str] = field(default_factory=list)   # added since the snapshot, and left alone
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)   # reasons it cannot be applied now
    plan_id: str = ""

    @property
    def commits(self) -> bool:
        """Whether applying the plan would create a git commit."""
        return any(i.commit for i in self.items)


@dataclass
class Outcome:
    """What an applied rollback did.

    Used by both rollback engines.

    Attributes:
        snapshot_id: The snapshot that was restored.
        branch: The branch that was committed to; None for plain folders.
        previous_head: The commit the branch was at before; None for plain folders.
        commit: The new commit, or None when no commit was made.
        backup_ref: Where what was replaced was saved, or None if nothing needed saving. A git ref for a git store; for
            a plain folder, a name in the same form that identifies a backup folder.
        items: The files that were changed.
        backup_path: Where the backup is, when it is a folder on disk (plain-folder stores) rather than a git ref.
    """

    snapshot_id: str
    branch: str | None
    previous_head: str | None
    commit: str | None
    backup_ref: str | None
    items: list[Item]
    backup_path: str | None = None   # where the backup is, when it is a folder on disk (plain-folder stores) rather than a git ref


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _plan_id(plan: Plan) -> str:
    body = {"snapshot": plan.snapshot_id, "store": plan.store, "head": plan.head, "branch": plan.branch,
            "items": [[i.path, i.action, i.source, _digest(i.target_bytes), _digest(i.live_bytes)] for i in plan.items]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def snapshot_text_problem(text: str) -> str | None:
    """Says why this snapshot text must not be written back into a file, or None if it is faithful.

    Refuses text that is only a size marker, that was cut, or that held undecodable bytes.
    """
    if text.startswith(_TOO_LARGE):
        return "the snapshot only holds a size marker for this file"
    if _CUT_MARKER.search(text):
        return "the snapshot holds only the start of this long file"
    if "\ufffd" in text:
        return "the original file had bytes that are not valid text, which the snapshot could not keep"
    return None


class Restorer(FileOps):
    """The rollback engine for a markdown/git store (see the module docstring for its rules).

    Use `plan` to see what a rollback would do, and `apply` to do it. Only `apply` writes, and it writes to the working
    tree, the git object store, the branch ref and the index of the adapter's repository.
    """

    def __init__(self, adapter: MarkdownGitAdapter):
        self._adapter = adapter
        self._git = adapter.git
        self._root = adapter.root

    # -- git helpers --------------------------------------------------------------------------------------------

    def _run(self, args: list[str], *, stdin: bytes | None = None, env: dict[str, str] | None = None,
             limit: int = 4_000_000) -> tuple[int, bytes, str]:
        with self._git.spawn(args, interactive=stdin is not None, env=env) as run:
            if stdin is not None:
                try:
                    run.inp.write(stdin)
                    run.inp.close()
                except OSError:
                    pass  # git exited early; its exit code and message explain why
            data = run.out.read(limit + 1)
            if len(data) > limit:
                run.stop()
                raise RestoreError("git produced unexpectedly large output")
            code = run.proc.wait()
            if run.timed_out:
                raise RestoreError(f"git timed out after {self._git.timeout:g} seconds")
            return code, data, run.stderr_text()

    def _must(self, args: list[str], what: str, **kw: Any) -> bytes:  # Any: passed on to _run (stdin, env, limit)
        code, data, err = self._run(args, **kw)
        if code != 0:
            raise RestoreError(f"git could not {what}: {safe_text(err, 200)}")
        return data

    # -- planning (read only) ---------------------------------------------------------------------------------

    def _git_state(self, plan: Plan) -> None:
        code, out, _ = self._run(["rev-parse", "--verify", "-q", "HEAD"])
        head = out.decode("ascii", "replace").strip()
        if code != 0 or not _SHA_RE.match(head):
            plan.blockers.append("the repository has no commits yet")
            return
        plan.head = head
        code, out, _ = self._run(["symbolic-ref", "-q", "HEAD"])
        branch = out.decode("utf-8", "replace").strip()
        if code != 0:
            plan.blockers.append("HEAD is detached; check out a branch first")
        elif not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith((".lock", "/", ".")) or "//" in branch:
            plan.blockers.append("the branch name is not one this tool will write to")
        else:
            plan.branch = branch
        names = list(_IN_PROGRESS) + ([f"{plan.branch}.lock"] if plan.branch else [])
        args = ["rev-parse"] + [part for name in names for part in ("--git-path", name)]
        code, out, _ = self._run(args)
        where = out.decode("utf-8", "replace").splitlines()
        if code == 0 and len(where) == len(names):
            for name, location in zip(names, where, strict=True):
                if os.path.lexists(os.path.join(str(self._root), location.strip())):
                    plan.blockers.append(f"git is busy or mid-operation ({safe_text(name, 40)} exists); finish or abort it first")
        else:
            plan.blockers.append("git could not say whether it is busy")
        code, _, _ = self._run(["diff-index", "--cached", "--quiet", "HEAD", "--"])
        if code == 1:
            plan.blockers.append("there are staged changes; commit or unstage them first")
        elif code != 0:
            plan.blockers.append("git could not compare the index with HEAD")

    def _head_entries(self, paths: list[str]) -> dict[str, tuple[str, str]]:
        """Mode and object id of each path as HEAD has it (("other", "") for anything that is not a plain file)."""
        found: dict[str, tuple[str, str]] = {}
        for start in range(0, len(paths), BATCH):
            chunk = paths[start:start + BATCH]
            code, out, _ = self._run(["ls-tree", "-z", "HEAD", "--", *chunk])
            if code != 0:
                continue
            for record in out.split(b"\0"):
                match = re.match(rb"^(\d{6}) (\w+) ([0-9a-f]{40,64})\t(.+)\Z", record, re.S)
                if match:
                    path = match.group(4).decode("utf-8", "replace")
                    found[path] = (match.group(1).decode(), match.group(3).decode()) if match.group(2) == b"blob" else ("other", "")
        return found

    def _history_blobs(self, paths: list[str], plan: Plan) -> dict[str, list[str]]:
        """For each path, the ids of the versions git has held, newest first. One git command per batch of files."""
        wanted = set(paths)
        versions: dict[str, list[str]] = {path: [] for path in paths}
        for start in range(0, len(paths), BATCH):
            chunk = paths[start:start + BATCH]
            try:
                code, out, _ = self._run(["log", "--format=", "--raw", "--no-renames", "--no-abbrev", "--no-color", "--no-ext-diff",
                                          "-n", str(HISTORY_COMMITS), "HEAD", "--", *chunk], limit=32_000_000)
            except RestoreError:
                plan.warnings.append("the history was too large to search for original versions; "
                                     "files are rebuilt from the snapshot's text where needed")
                continue
            if code != 0:
                continue
            for line in out.decode("utf-8", "replace").splitlines():
                match = _RAW_RE.match(line)
                if not match:
                    continue
                path, new_sha, status = match.group(6), match.group(4), match.group(5)
                if path in wanted and status in ("A", "M") and set(new_sha) != {"0"} and len(versions[path]) < MAX_HISTORY_PER_FILE:
                    versions[path].append(new_sha)
        return versions

    @staticmethod
    def _exact_source(blobs: _BlobReader, candidates: list[str], wanted: str) -> tuple[str, bytes] | None:
        """The newest git version of this file whose text equals the snapshot's text, with its exact bytes."""
        seen: set[str] = set()
        for sha in candidates:
            if sha in seen:
                continue
            seen.add(sha)
            data = blobs.raw(sha)
            if data is not None and _text_from_bytes(data) == wanted:
                return sha, data
        return None

    def plan(self, snapshot: Snapshot, *, only: Sequence[str] = (), remove_added: bool = False) -> Plan:
        """Compares the snapshot with the repository and reports what a rollback would do. Writes nothing.

        Only runs read-only git commands and reads the working tree. Unsafe states (a detached HEAD, staged changes, an
        operation in progress, a lock file, names that differ only by letter case, too many files) are reported as
        blockers on the returned plan, and files that cannot be safely written are listed in `skipped`.

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
        self._git_state(plan)
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
        differing: list[tuple[str, str, bytes | None, str | None]] = []
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
            differing.append((rel, memory.text, live_bytes, live_text))

        removals: list[tuple[str, bytes]] = []
        if remove_added and live_complete:
            for rel in sorted(live_ids - set(by_path)):
                if chosen is not None and rel not in chosen:
                    continue
                status, live_bytes = self._inspect(rel)
                if status != "ok":
                    plan.skipped.append((rel, "added since, but cannot be safely read, so it is left alone"))
                    continue
                removals.append((rel, live_bytes or b""))
        elif not remove_added:
            live = adapter.list_memories({"store": adapter.store})
            plan.kept_new = sorted(m.id for m in live.memories if m.id not in by_path and (chosen is None or m.id in chosen))

        wanted_paths = [d[0] for d in differing]
        heads = self._head_entries(wanted_paths + [r[0] for r in removals])
        history = self._history_blobs(wanted_paths, plan) if wanted_paths else {}
        with _BlobReader(self._git) as blobs:
            for rel, text, live_bytes, live_text in differing:
                item = self._plan_write(blobs, rel, text, live_bytes, live_text, plan, heads.get(rel), history.get(rel, []))
                if item is not None:
                    total += len(item.target_bytes or b"") + len(item.live_bytes or b"")
                    plan.items.append(item)
            for rel, live_bytes in removals:
                item = self._plan_remove(blobs, rel, live_bytes, heads.get(rel))
                if item is not None:
                    total += len(item.live_bytes or b"")
                    plan.items.append(item)

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

    def _plan_write(self, blobs: _BlobReader, rel: str, text: str, live_bytes: bytes | None, live_text: str | None,
                    plan: Plan, entry: tuple[str, str] | None, candidates: list[str]) -> Item | None:
        if entry is not None and entry[0] not in _REGULAR:
            plan.skipped.append((rel, "git tracks it as a link or submodule; never written"))
            return None
        head_mode, head_bytes, head_sha = None, None, None
        if entry is not None:
            head_mode, head_sha = entry[0], entry[1]
            head_bytes = blobs.raw(entry[1])
            if head_bytes is None:
                plan.skipped.append((rel, "the committed version could not be read"))
                return None
        found = self._exact_source(blobs, candidates, text)
        if found is not None:
            sha, data = found
            source, note = "git", f"original bytes from git {sha[:10]}"
        else:
            problem = snapshot_text_problem(text)
            if problem is not None:
                plan.skipped.append((rel, f"no matching version in git history, and {problem}"))
                return None
            sha, data, source = None, text.encode("utf-8"), "snapshot"
            note = "rebuilt from the snapshot's text (no matching version in git; line endings are normalised)"
        action = "restore" if live_bytes is not None else "recreate"
        commit = head_bytes is None or data != head_bytes
        backup = live_bytes is not None and (head_bytes is None or live_bytes != head_bytes)
        if backup:
            note += "; replaces an uncommitted edit (saved first)"
        return Item(rel, action, source, live_bytes, live_text, data, text, sha, head_mode, head_bytes, commit, backup, note,
                    head_sha)

    def _plan_remove(self, blobs: _BlobReader, rel: str, live_bytes: bytes | None, entry: tuple[str, str] | None) -> Item | None:
        head_mode, head_bytes, head_sha = None, None, None
        if entry is not None:
            if entry[0] not in _REGULAR:
                return None
            head_mode, head_sha, head_bytes = entry[0], entry[1], blobs.raw(entry[1])
            if head_bytes is None:
                return None
        backup = live_bytes is not None and (head_bytes is None or live_bytes != head_bytes)
        note = "added since the snapshot" + ("; never committed, saved first" if backup and head_bytes is None
                                              else "; has uncommitted edits, saved first" if backup else "")
        return Item(rel, "remove", "none", live_bytes, None if live_bytes is None else _text_from_bytes(live_bytes),
                    None, None, None, head_mode, head_bytes, commit=head_bytes is not None, backup=backup, note=note,
                    head_sha=head_sha)

    # -- applying -------------------------------------------------------------------------------------------------

    def apply(self, snapshot: Snapshot, *, expected_plan_id: str, only: Sequence[str] = (), remove_added: bool = False,
              now: datetime | None = None) -> Outcome:
        """Carry out the plan the person confirmed. Re-plans first and refuses if anything has changed since.

        This is the only method that writes. It first saves anything it would overwrite or delete that git does not
        already hold (under refs/memdebug/backups/), then makes one new commit on the branch if any change must be
        committed, then updates the index and the working tree. If any step fails, the files, the branch and the index
        are put back (a backup ref that was already made is kept).

        Args:
            snapshot: The snapshot to restore.
            expected_plan_id: The `plan_id` of the plan the person confirmed.
            only: The same restriction that was given to `plan`.
            remove_added: The same setting that was given to `plan`.
            now: The time to record in the commit and the backup name; defaults to the current time.

        Returns:
            What was done. If the plan has nothing to change, nothing is written and the outcome has no commit.

        Raises:
            RestoreError: If the new plan has blockers, differs from the confirmed plan, or a step fails. The message
                says whether the rollback was undone; if it could not be fully undone, it lists what remains and names
                the backup ref.
        """
        now = now or datetime.now(timezone.utc)
        plan = self.plan(snapshot, only=only, remove_added=remove_added)
        if plan.blockers:
            raise RestoreError("cannot roll back right now: " + "; ".join(plan.blockers) + ". Nothing was changed.")
        if plan.plan_id != expected_plan_id:
            raise RestoreError("the repository changed after the plan was made. Nothing was changed; run it again.")
        if not plan.items:
            return Outcome(plan.snapshot_id, plan.branch, plan.head, None, None, [])
        assert plan.head is not None and plan.branch is not None

        for item in plan.items:  # every write is checked before the first one happens
            if item.action != "remove":
                self._check_target(item.path)
            elif item.live_bytes is not None:
                self._check_target(item.path, must_exist=True)

        backup_ref = self._save_backup([i for i in plan.items if i.backup], plan, now) if any(i.backup for i in plan.items) else None

        entries: list[tuple[str, str | None, str]] = []   # (mode, blob id or None to delete, path)
        undo_entries: list[tuple[str, str | None, str]] = []
        for item in plan.items:
            if not item.commit:
                continue
            if item.action == "remove":
                entries.append(("0", None, item.path))
            else:
                sha = item.target_sha or self._store_blob(item.target_bytes or b"")
                entries.append((item.head_mode or "100644", sha, item.path))
            undo_entries.append((item.head_mode or "0", item.head_sha, item.path) if item.head_sha else ("0", None, item.path))
        commit = self._build_commit(plan, entries, now) if entries else None

        journal: list[tuple[str, bytes | None]] = []
        created_dirs: list[str] = []
        moved = {"ref": False, "index": False}
        try:
            if commit is not None:
                self._must(["update-ref", "-m", f"memdebug: restore to {plan.snapshot_id}", plan.branch, commit, plan.head],
                           "move the branch", env=self._identity(now))
                moved["ref"] = True
                self._must(["update-index", "-z", "--index-info"], "update the index", stdin=self._index_info(plan, entries))
                moved["index"] = True
            for item in plan.items:
                if item.action == "remove":
                    self._remove_file(item.path)
                    journal.append((item.path, item.live_bytes))
                else:
                    created_dirs += self._write_file(item.path, item.target_bytes or b"")
                    journal.append((item.path, item.live_bytes))
            self._verify(plan, commit)
        except BaseException as failure:
            problems = self._undo(plan, journal, created_dirs, moved, undo_entries, commit)
            where = f" Your previous content is saved in {backup_ref}." if backup_ref else ""
            if problems:
                raise RestoreError(
                    f"the rollback failed ({safe_text(failure, 160)}) and could not be fully undone "
                    f"({'; '.join(problems)}). The branch was at {plan.head}.{where}") from failure
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise
            raise RestoreError(f"the rollback failed ({safe_text(failure, 200)}) and was undone; nothing was changed.") from failure
        return Outcome(plan.snapshot_id, plan.branch, plan.head, commit, backup_ref, plan.items)

    @staticmethod
    def _index_info(plan: Plan, entries: list[tuple[str, str | None, str]]) -> bytes:
        zeros = "0" * len(plan.head or "0" * 40)
        return b"".join(f"{mode} {sha or zeros}\t{path}\0".encode("utf-8") for mode, sha, path in entries)

    @staticmethod
    def _identity(now: datetime) -> dict[str, str]:
        when = f"{int(now.timestamp())} +0000"
        return {"GIT_AUTHOR_NAME": COMMIT_NAME, "GIT_AUTHOR_EMAIL": COMMIT_EMAIL, "GIT_AUTHOR_DATE": when,
                "GIT_COMMITTER_NAME": COMMIT_NAME, "GIT_COMMITTER_EMAIL": COMMIT_EMAIL, "GIT_COMMITTER_DATE": when}

    def _store_blob(self, data: bytes) -> str:
        sha = self._must(["hash-object", "-w", "--no-filters", "--stdin"], "store a file", stdin=data).decode().strip()
        if not _SHA_RE.match(sha):
            raise RestoreError("git returned an unexpected object id")
        return sha

    def _in_private_index(self, work: Callable[[dict[str, str]], str]) -> str:
        """Run `work(env)` with a private index file (so the real index and working tree are untouched); returns its result."""
        folder = tempfile.mkdtemp(prefix="memdebug-")
        try:
            return work({"GIT_INDEX_FILE": os.path.join(folder, "index")})
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _build_commit(self, plan: Plan, entries: list[tuple[str, str | None, str]], now: datetime) -> str:
        head = plan.head
        if head is None:
            raise RestoreError("there is no commit to build on")
        paths = [f"{item.action} {item.path}" for item in plan.items if item.commit]
        message = (f"memdebug: restore {len(paths)} memory file{'' if len(paths) == 1 else 's'} to snapshot "
                   f"{plan.snapshot_id}\n\n" + "\n".join(paths[:100]) + ("\n..." if len(paths) > 100 else ""))

        def work(env: dict[str, str]) -> str:
            self._must(["read-tree", head], "read the current tree", env=env)
            self._must(["update-index", "-z", "--index-info"], "prepare the new tree", env=env,
                       stdin=self._index_info(plan, entries))
            tree = self._must(["write-tree"], "write the new tree", env=env).decode().strip()
            commit = self._must(["commit-tree", tree, "-p", head, "-m", message], "create the commit",
                                env={**env, **self._identity(now)}).decode().strip()
            if not _SHA_RE.match(tree) or not _SHA_RE.match(commit):
                raise RestoreError("git returned an unexpected object id")
            return commit

        return self._in_private_index(work)

    def _save_backup(self, items: list[Item], plan: Plan, now: datetime) -> str:
        """Keep what is about to be overwritten or deleted, as a commit nothing else points to."""
        ref = f"refs/memdebug/backups/{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        entries: list[tuple[str, str | None, str]] = [("100644", self._store_blob(item.live_bytes or b""), item.path) for item in items]

        def work(env: dict[str, str]) -> str:
            self._must(["update-index", "-z", "--index-info"], "prepare the backup", env=env,
                       stdin=self._index_info(plan, entries))
            tree = self._must(["write-tree"], "write the backup", env=env).decode().strip()
            message = f"memdebug: content replaced by the rollback to {plan.snapshot_id}\n\n" + "\n".join(i.path for i in items[:100])
            return self._must(["commit-tree", tree, "-m", message], "create the backup",
                              env={**env, **self._identity(now)}).decode().strip()

        commit = self._in_private_index(work)
        self._must(["update-ref", ref, commit, "0" * len(commit)], "keep the backup", env=self._identity(now))
        return ref

    # -- working tree -----------------------------------------------------------------------------------------------

    def _verify(self, plan: Plan, commit: str | None) -> None:
        """The store must now hold exactly what the plan said, and git must agree with the working tree."""
        self._verify_files(plan.items)
        if commit is not None:
            code, out, _ = self._run(["rev-parse", "--verify", "-q", "HEAD"])
            if code != 0 or out.decode().strip() != commit:
                raise RestoreError("the branch does not point at the new commit")
            code, _, _ = self._run(["diff-index", "--cached", "--quiet", "HEAD", "--"])
            if code != 0:
                raise RestoreError("the index does not match the new commit")

    def _undo(
        self, plan: Plan, journal: list[tuple[str, bytes | None]], created_dirs: list[str], moved: dict[str, bool],
        undo_entries: list[tuple[str, str | None, str]], commit: str | None,
    ) -> list[str]:
        """Put everything back, newest step first. Returns what could not be undone."""
        problems = self._undo_files(journal, created_dirs)
        if moved["index"]:
            try:
                self._must(["update-index", "-z", "--index-info"], "restore the index", stdin=self._index_info(plan, undo_entries))
            except Exception as exc:
                problems.append(f"index: {safe_text(exc, 80)}")
        if moved["ref"] and commit is not None and plan.branch and plan.head:
            try:
                self._must(["update-ref", "-m", "memdebug: undo failed rollback", plan.branch, plan.head, commit], "restore the branch")
            except Exception as exc:
                problems.append(f"branch: {safe_text(exc, 80)}")
        return problems
