"""The `memdebug` command: a Typer application with one function per command.

Commands only call into the rest of the package and print the result.
Everything shown goes through `echo`, which escapes text the console cannot
show. `guarded` turns expected errors into a short message and exit code 2.
"""
import functools
import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NoReturn, Optional, ParamSpec, TypeVar, cast

import typer

from . import longpath
from .adapters.base import MemoryAdapter
from .adapters.folder import FolderAdapter
from .adapters.folder_restore import FolderRestorer
from .adapters.markdown_git import MarkdownGitAdapter
from .adapters.mem0 import Mem0Adapter, build_mem0_memory, validate_scope
from .adapters.openwebui import OpenWebUIAdapter
from .adapters.restore import Plan, Restorer
from .agents import scan_agents
from .agents import summary_lines as agent_lines
from .backups import Backup, BackupError, list_backups, select
from .backups import remove as remove_backup
from .describe import describe_event
from .diff import Diff, diff_snapshots, line_diff
from .docker_source import valid_container
from .errors import MemdebugError
from .ledger import Ledger
from .models import Snapshot
from .monitor import alarms_since_snapshot, baseline, check_all, check_store, status_lines, store_hints, summary_lines, watch
from .paths import backup_root_for, default_ledger_path
from .report import RENDERERS, build_report, write_report
from .rollback_flow import Restoring, run_rollback
from .stores import (
    KIND_NAMES,
    KINDS,
    Registry,
    StoreConfig,
    detect_kind,
    discover_docker,
    load_registry,
    open_store,
    save_registry,
    valid_name,
)
from .sync import sync
from .textsafe import console_safe, safe_text
from .witness import SAME_DISK_WARNING, append_witness, same_disk, verify_witness

app = typer.Typer(help="Inspect, compare and roll back what an AI agent's memory holds. Local-first and agent-neutral.", no_args_is_help=True)


def _show_version(show: bool) -> None:
    if show:
        from . import __version__

        typer.echo(f"memdebug {__version__}")
        raise typer.Exit()


@app.callback()
def _main(version: bool = typer.Option(False, "--version", callback=_show_version, is_eager=True, help="Show the version and exit.")) -> None:
    pass

DB_OPTION = typer.Option(
    None, "--db", help="Path to the ledger file (default: a per-user data folder, see 'memdebug where')."
)


def _open_ledger(db: Optional[Path]) -> Ledger:
    if db is None:
        db = default_ledger_path()
        try:
            db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise MemdebugError(f"cannot create the default ledger folder: {exc.strerror}") from exc
    return Ledger(db)


def echo(text: str, **kwargs: Any) -> None:
    """Prints text to the console, escaping what it cannot show.

    Args:
        text: What to print. Characters the console's encoding cannot show
            become visible escapes instead of raising.
        **kwargs: Passed on to typer.echo (for example err=True).
    """
    typer.echo(console_safe(text), **kwargs)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def guarded(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Turns expected errors into a clean message and exit code.

    Also hides tracebacks that could expose paths or values unless
    MEMDEBUG_DEBUG is set.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except MemdebugError as exc:
            echo(f"error: {safe_text(exc, 500)}", err=True)
            raise typer.Exit(2) from None
        except (typer.Exit, typer.Abort, KeyboardInterrupt):
            raise
        except Exception as exc:
            if os.environ.get("MEMDEBUG_DEBUG"):
                raise
            echo(
                f"unexpected error ({type(exc).__name__}); set MEMDEBUG_DEBUG=1 to see details",
                err=True,
            )
            raise typer.Exit(70) from None

    return wrapper


@app.command()
@guarded
def timeline(db: Path = DB_OPTION, limit: int = typer.Option(50, min=1, max=10_000, help="Newest entries to show.")) -> None:
    """Show memory events, newest first. Memory text is shown escaped, never raw."""
    entries = _open_ledger(db).entries()[::-1][:limit]
    if not entries:
        echo("The ledger is empty.")
        return
    for entry in entries:
        event = entry.event
        text = describe_event(event)
        flag = "  [untrusted source]" if event.trust.value == "untrusted" else ""
        echo(
            f"{safe_text(entry.id, 12):>6}  {event.ts:%Y-%m-%d %H:%M}  {event.op.value:<8} "
            f"{safe_text(event.memory_id, 36)}  {safe_text(text, 60)}{flag}"
        )


@app.command()
@guarded
def verify(
    db: Path = DB_OPTION,
    expected_head: Optional[str] = typer.Option(None, help="Head hash recorded in a snapshot."),
    witness: Optional[Path] = typer.Option(None, "--witness", help="Also check the ledger against this witness file."),
) -> None:
    """Check that the ledger has not been edited (and, with --witness, not rewritten or cut short)."""
    ledger = _open_ledger(db)
    result = ledger.verify(expected_head)
    failed = False
    if result.ok:
        echo("Ledger intact.")
    else:
        failed = True
        for problem in result.problems:
            echo(f"PROBLEM: {problem}")
    if witness is not None:
        check = verify_witness(ledger, witness, ledger_path=db or default_ledger_path())
        for note in check.warnings:
            echo(f"warning: {note}", err=True)
        if check.ok:
            echo(f"Witness agrees: the ledger still holds all {check.lines} witnessed head(s); "
                 f"{check.unwitnessed} newer entr{'y' if check.unwitnessed == 1 else 'ies'} not yet witnessed.")
        else:
            failed = True
            for problem in check.problems:
                echo(f"PROBLEM: {safe_text(problem, 300)}")
    if failed:
        raise typer.Exit(1)


@app.command("report")
@guarded
def report_command(
    fmt: str = typer.Option("markdown", "--format", "-f", help="markdown (for people), json (for programs) or sarif (for CI and code scanning)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write to this file instead of the screen."),
    force: bool = typer.Option(False, "--force", help="Replace the --out file if it exists."),
    witness: Optional[Path] = typer.Option(None, "--witness", help="Also check the ledger against this witness file."),
    fail_on: str = typer.Option("none", "--fail-on", help="'findings': exit 1 if anything needs a look; 'hints': also if any wording looks worth a second look (for CI). Default: only a failed integrity check does."),
    db: Path = DB_OPTION,
) -> None:
    """A report on the ledger: integrity, what changed outside the store's own history, rollbacks and snapshots."""
    if fmt not in RENDERERS or fail_on not in ("none", "findings", "hints"):
        raise MemdebugError("--format must be markdown, json or sarif, and --fail-on must be none, findings or hints")
    ledger = _open_ledger(db)
    check = verify_witness(ledger, witness, ledger_path=db or default_ledger_path()) if witness is not None else None
    report = build_report(ledger, ledger_name=(db or default_ledger_path()).name, witness=check)
    text = RENDERERS[fmt](report)
    if out is None:
        echo(text, nl=False)
    else:
        write_report(out, text, force=force)
        echo(f"Report written: {out}")
    if not report.integrity_ok or (check is not None and not check.ok) or (fail_on in ("findings", "hints") and report.attention) or (fail_on == "hints" and report.hinted):
        raise typer.Exit(1)


@app.command("witness")
@guarded
def witness_command(
    file: Path = typer.Option(..., "--file", help="The witness file (created if missing). Keep it away from the ledger."),
    db: Path = DB_OPTION,
) -> None:
    """Record the ledger's newest entry in a witness file kept somewhere else, so rewriting or cutting the ledger is noticed."""
    ledger = _open_ledger(db)
    if not ledger.verify().ok:
        raise MemdebugError("the ledger has problems; run 'memdebug verify' first. Nothing was witnessed")
    line, new = append_witness(ledger, file)
    echo(f"{'Witnessed' if new else 'Already witnessed'}: entry {line.seq} of {line.count}, head {line.head[:16]}...")
    if same_disk(db or default_ledger_path(), file):
        echo(f"warning: {SAME_DISK_WARNING}", err=True)
    echo("Check it any time with: memdebug verify --witness <that file>")


sync_app = typer.Typer(
    help="Copy a backend's history into the ledger and look for edits made outside its own history.",
    no_args_is_help=True,
)
app.add_typer(sync_app, name="sync")

SETTLE_OPTION = typer.Option(1.0, min=0.0, max=60.0, help="Seconds between the two confirming passes.")
ADOPT_OPTION = typer.Option(False, help="First sync only: record memories without history as ADDs.")


def _run_sync(adapter: MemoryAdapter, ledger: Ledger, scope: dict[str, str], adopt_existing: bool, settle: float, notes: list[str]) -> None:
    report = sync(adapter, ledger, scope, adopt_existing=adopt_existing, settle_seconds=settle)
    for note in notes + report.warnings:
        echo(f"warning: {safe_text(note, 300)}", err=True)
    echo(
        f"Added {report.history_events} history event(s) and {report.external_events} "
        f"change(s) made outside the backend's own history. Ledger head: {ledger.head()}"
    )


MEM0_HISTORY_OPTION = typer.Option(..., "--mem0-history-db", help="Mem0's history.db (opened read-only).")
MEM0_CONFIG_OPTION = typer.Option(None, "--mem0-config", help="JSON config for mem0 (never printed).")
MD_PATH_OPTION = typer.Option(..., "--path", help="Top folder of the git repository holding the markdown memories.")
MD_STORE_OPTION = typer.Option(None, "--store", help="A name for this memory store (default: folder name).")
MD_SUBDIR_OPTION = typer.Option(None, "--subdir", help="Only read this folder inside the repository.")


def _setup_mem0(
    history_db: Path, config: Optional[Path], user_id: Optional[str], agent_id: Optional[str], run_id: Optional[str]
) -> tuple[Mem0Adapter, dict[str, str], list[str]]:
    scope = validate_scope(
        {k: v for k, v in (("user_id", user_id), ("agent_id", agent_id), ("run_id", run_id)) if v is not None}
    )
    memory, notes = build_mem0_memory(config)
    return Mem0Adapter(memory, history_db), scope, notes


def _setup_markdown(path: Path, store: Optional[str], subdir: Optional[str]) -> tuple[MarkdownGitAdapter, dict[str, str], list[str]]:
    adapter = MarkdownGitAdapter(path, store=store, subdir=subdir)
    return adapter, {"store": adapter.store}, []


@sync_app.command("mem0")
@guarded
def sync_mem0(
    mem0_history_db: Path = MEM0_HISTORY_OPTION,
    db: Path = DB_OPTION,
    mem0_config: Optional[Path] = MEM0_CONFIG_OPTION,
    user_id: Optional[str] = typer.Option(None, "--user-id"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
    adopt_existing: bool = ADOPT_OPTION,
    settle: float = SETTLE_OPTION,
) -> None:
    """Self-hosted Mem0: history from its history.db, live memories through its own API."""
    adapter, scope, notes = _setup_mem0(mem0_history_db, mem0_config, user_id, agent_id, run_id)
    _run_sync(adapter, _open_ledger(db), scope, adopt_existing, settle, notes)


@sync_app.command("markdown")
@guarded
def sync_markdown(
    path: Path = MD_PATH_OPTION,
    db: Path = DB_OPTION,
    store: Optional[str] = MD_STORE_OPTION,
    subdir: Optional[str] = MD_SUBDIR_OPTION,
    adopt_existing: bool = ADOPT_OPTION,
    settle: float = SETTLE_OPTION,
) -> None:
    """Sync markdown notes in a git repository into the ledger.

    History comes from git log and live memories from the working tree.
    Uncommitted edits show up as changes made outside the history.
    """
    adapter, scope, notes = _setup_markdown(path, store, subdir)
    _run_sync(adapter, _open_ledger(db), scope, adopt_existing, settle, notes)


# -- snapshots and compare -------------------------------------------------------------------------------

snapshot_app = typer.Typer(
    help="Save what a memory store holds right now, so it can be compared later.", no_args_is_help=True
)
app.add_typer(snapshot_app, name="snapshot")

LABEL_OPTION = typer.Option(None, "--label", help="A short note for this snapshot (up to 100 characters).")
DIFF_FROM_OPTION = typer.Option(None, "--diff-from", help="Also show what changed since this snapshot (for example s1).")


def _scope_text(scope: dict) -> str:
    return ", ".join(f"{safe_text(k, 20)}={safe_text(v, 30)}" for k, v in sorted(scope.items())) or "-"


def _print_diff(result: Diff, full: bool = False, limit: int = 200) -> None:
    echo(
        f"{result.old.id} -> {result.new.id}  ({safe_text(result.old.backend, 30)}, {_scope_text(result.old.scope)}): "
        f"{result.count('changed')} changed, {result.count('added')} added, {result.count('removed')} removed"
    )
    for note in result.warnings:
        echo(f"warning: {safe_text(note, 300)}", err=True)
    symbols = {"changed": "~", "added": "+", "removed": "-"}
    for change in result.changes[:limit]:
        name = safe_text(change.memory_id, 36)
        if change.kind == "changed":
            detail = f"{safe_text(change.before, 40)} -> {safe_text(change.after, 40)}"
        else:
            detail = safe_text(change.after if change.kind == "added" else change.before, 60)
        echo(f"{symbols[change.kind]} {name}  {detail}")
        if full and change.kind == "changed":
            for line in line_diff(change.before, change.after):
                echo(f"    {safe_text(line, 200)}")
    if len(result.changes) > limit:
        echo(f"... and {len(result.changes) - limit} more (use --limit to show more)")


def _run_snapshot(
    adapter: MemoryAdapter, ledger: Ledger, scope: dict[str, str], notes: list[str], settle: float, label: Optional[str],
    diff_from: Optional[str], adopt_existing: bool = False,
) -> None:
    previous = ledger.load_snapshot(diff_from) if diff_from else None  # fail before doing any work
    report = sync(adapter, ledger, scope, adopt_existing=adopt_existing, settle_seconds=settle)
    live = report.live
    if live is None:  # cannot happen with a normal sync; never snapshot something unchecked
        raise MemdebugError("the sync did not produce a listing to snapshot")
    for note in notes + report.warnings:
        echo(f"warning: {safe_text(note, 300)}", err=True)
    info = ledger.save_snapshot(
        adapter.name, scope, live.memories, complete=live.complete,
        taken_at=datetime.now(timezone.utc), label=label,
    )
    echo(
        f"Saved snapshot {info.id}: {info.count} memories"
        + ("" if info.complete else " (the listing may be incomplete; comparisons will not claim additions or removals from it)")
        + f". Ledger head: {ledger.head()}"
    )
    if previous is not None:
        _print_diff(diff_snapshots(previous, ledger.load_snapshot(info.id)))


@snapshot_app.command("mem0")
@guarded
def snapshot_mem0(
    mem0_history_db: Path = MEM0_HISTORY_OPTION,
    db: Path = DB_OPTION,
    mem0_config: Optional[Path] = MEM0_CONFIG_OPTION,
    user_id: Optional[str] = typer.Option(None, "--user-id"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
    label: Optional[str] = LABEL_OPTION,
    diff_from: Optional[str] = DIFF_FROM_OPTION,
    adopt_existing: bool = ADOPT_OPTION,
    settle: float = SETTLE_OPTION,
) -> None:
    """Sync, then save a snapshot of a self-hosted Mem0 store."""
    adapter, scope, notes = _setup_mem0(mem0_history_db, mem0_config, user_id, agent_id, run_id)
    _run_snapshot(adapter, _open_ledger(db), scope, notes, settle, label, diff_from, adopt_existing)


@snapshot_app.command("markdown")
@guarded
def snapshot_markdown(
    path: Path = MD_PATH_OPTION,
    db: Path = DB_OPTION,
    store: Optional[str] = MD_STORE_OPTION,
    subdir: Optional[str] = MD_SUBDIR_OPTION,
    label: Optional[str] = LABEL_OPTION,
    diff_from: Optional[str] = DIFF_FROM_OPTION,
    adopt_existing: bool = ADOPT_OPTION,
    settle: float = SETTLE_OPTION,
) -> None:
    """Sync, then save a snapshot of markdown memory files in a git repository."""
    adapter, scope, notes = _setup_markdown(path, store, subdir)
    _run_snapshot(adapter, _open_ledger(db), scope, notes, settle, label, diff_from, adopt_existing)


@snapshot_app.command("store")
@guarded
def snapshot_store(
    name: str = typer.Argument(..., help="The name of a watched store (see 'memdebug stores')."),
    label: Optional[str] = typer.Option(None, "--label", help="A short note about this snapshot (default: 'saved by hand')."),
    include_changes: bool = typer.Option(False, "--include-changes", help="Save it even though the ledger holds changes you have not reviewed."),
    settle: float = SETTLE_OPTION,
    db: Path = DB_OPTION,
) -> None:
    """Save a snapshot of a watched store as it is now.

    That is what 'memdebug rollback store' can put back later. A snapshot is
    what a rollback treats as good, so this refuses when the ledger holds an
    outside-history change or suspicious wording recorded since the store's
    last snapshot: look at that first ('memdebug serve'), then add
    --include-changes if it is fine.
    """
    cfg = _watched_store(name, db)
    if label is not None and not 0 < len(label) <= 100:
        raise MemdebugError("the label must be 1 to 100 characters")
    ledger = _open_ledger(db)
    result = check_store(cfg, ledger, settle=settle)  # look first, so everything up to this moment is in the ledger
    if result.error:
        raise MemdebugError(result.error)
    opened = open_store(cfg, refresh=False)
    flagged = alarms_since_snapshot(ledger, opened.adapter.name, opened.scope)
    if flagged and not include_changes:
        echo(f"Not saved. Since the last snapshot of {safe_text(cfg.name, 40)}, the ledger recorded changes worth a second look:")
        for memory_id, why in flagged[:8]:
            echo(f"  {safe_text(memory_id, 60)}: {safe_text(why, 120)}")
        if len(flagged) > 8:
            echo(f"  ... and {len(flagged) - 8} more")
        echo("A snapshot is what a rollback later treats as good, so look at these first ('memdebug serve').")
        echo("If they are fine, run this again with --include-changes.")
        raise typer.Exit(1)
    snapshot_id = baseline(cfg, ledger, label or "saved by hand", refresh=False)
    echo(f"Saved snapshot {snapshot_id} of {safe_text(cfg.name, 40)}.")
    if cfg.kind in ("markdown", "folder"):
        echo(f"To put the store back to it later: memdebug rollback store {cfg.name} --to {snapshot_id}")
    else:
        echo("This store can be compared with it ('memdebug diff'), but not rolled back: memdebug only ever reads it.")


@snapshot_app.command("list")
@guarded
def snapshot_list(db: Path = DB_OPTION) -> None:
    """List saved snapshots."""
    infos = _open_ledger(db).list_snapshots()
    if not infos:
        echo("No snapshots yet.")
        return
    for info in infos:
        label = f"  {safe_text(info.label, 60)}" if info.label else ""
        echo(
            f"{info.id:>5}  {info.taken_at:%Y-%m-%d %H:%M}  {safe_text(info.backend, 14):<12} "
            f"{_scope_text(info.scope)}  {info.count} memories"
            f"{'' if info.complete else '  (incomplete)'}  after e{info.ledger_seq}{label}"
        )


@snapshot_app.command("delete")
@guarded
def snapshot_delete(
    snapshot_id: str = typer.Argument(..., help="For example s1."),
    db: Path = DB_OPTION,
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Delete a snapshot. The deletion is recorded in the ledger; the texts only that snapshot used are removed."""
    if not yes:
        typer.confirm(f"Delete snapshot {safe_text(snapshot_id, 12)}? This cannot be undone.", abort=True)
    info = _open_ledger(db).delete_snapshot(snapshot_id)
    echo(f"Deleted snapshot {info.id}.")


@app.command("diff")
@guarded
def diff_command(
    old: str = typer.Argument(..., help="The earlier snapshot, for example s1."),
    new: str = typer.Argument(..., help="The later snapshot, for example s2."),
    db: Path = DB_OPTION,
    full: bool = typer.Option(False, "--full", help="Show a line-by-line diff of each changed memory."),
    limit: int = typer.Option(200, min=1, max=100_000, help="Most changes to list."),
) -> None:
    """Show what changed between two snapshots."""
    ledger = _open_ledger(db)
    _print_diff(diff_snapshots(ledger.load_snapshot(old), ledger.load_snapshot(new)), full=full, limit=limit)


@app.command()
@guarded
def where() -> None:
    """Show where the default ledger is kept."""
    echo(str(default_ledger_path()))


@app.command()
@guarded
def selftest() -> None:
    """Check on THIS machine that the safety protections work.

    That covers git isolation, read-only access, killing a hung git, symlink
    and junction handling, and path rules. Run it once on every new platform,
    especially Windows.
    """
    from .selftest import run_all

    results = run_all()
    failed = 0
    for name, status, detail in results:
        echo(f"[{status:<4}] {name}" + (f": {detail}" if detail else ""))
        failed += status == "FAIL"
    if failed:
        echo(f"{failed} check(s) FAILED. Do not rely on this tool on this machine until that is understood.", err=True)
        raise typer.Exit(1)
    echo("All checks passed (SKIP means the check could not be run here, so it proves nothing).")


# -- rollback -----------------------------------------------------------------------------------------------

rollback_app = typer.Typer(
    help="Put a memory store back to what a snapshot held. Shows what would change first; nothing is written without --apply.",
    no_args_is_help=True,
)
app.add_typer(rollback_app, name="rollback")


def _print_plan(plan: Plan, snapshot_label: str, full: bool) -> None:
    echo(f"Rollback of \"{safe_text(plan.store, 40)}\" to {plan.snapshot_id}{snapshot_label}")
    if plan.branch:
        echo(f"Branch {safe_text(plan.branch.removeprefix('refs/heads/'), 60)} is at {(plan.head or '')[:12]}.")
    echo("")
    verbs = {"restore": "~ restore ", "recreate": "+ recreate", "remove": "- remove  "}
    for item in plan.items:
        echo(f"  {verbs[item.action]} {safe_text(item.path, 70)}")
        echo(f"      {safe_text(item.note, 200)}")
        if full and item.action != "remove":
            for line in line_diff(item.live_text or "", item.target_text or "")[:60]:
                echo(f"        {safe_text(line, 160)}")
    if not plan.items:
        if plan.snapshot_complete:
            echo("  Nothing to restore: every file in scope already matches the snapshot.")
        else:
            echo("  Nothing to restore among the files the snapshot holds. That snapshot is incomplete (some notes could not be read when it")
            echo("  was taken), so it cannot vouch for the rest: a note missing from it could not be put back.")
    if plan.kept_new:
        shown = ", ".join(safe_text(n, 40) for n in plan.kept_new[:5]) + (" ..." if len(plan.kept_new) > 5 else "")
        echo(f"\nAdded since the snapshot and left alone: {shown}  (use --remove-added to remove them)")
    for name, reason in plan.skipped:
        echo(f"\nSkipped {safe_text(name, 60)}: {safe_text(reason, 200)}")
    for note in plan.warnings:
        echo(f"\nwarning: {safe_text(note, 300)}", err=True)
    if plan.items:
        saved = sum(1 for i in plan.items if i.backup)
        echo(f"\n{len(plan.items)} file(s) would change"
             + (", in one new commit" if plan.commits else ", with no new commit" if plan.branch else ", changed in place")
             + (f"; {saved} would be saved to a backup first" if saved else "") + ".")
    for reason in plan.blockers:
        echo(f"\nCannot be applied now: {safe_text(reason, 300)}")


@rollback_app.command("markdown")
@guarded
def rollback_markdown(
    to: str = typer.Option(..., "--to", help="The snapshot to go back to (for example s1)."),
    path: Path = MD_PATH_OPTION,
    db: Path = DB_OPTION,
    store: Optional[str] = MD_STORE_OPTION,
    subdir: Optional[str] = MD_SUBDIR_OPTION,
    only: Optional[list[str]] = typer.Option(None, "--only", help="Restore only this file (repeat for several)."),
    remove_added: bool = typer.Option(False, "--remove-added", help="Also remove files added since the snapshot."),
    full: bool = typer.Option(False, "--full", help="Show the line changes for each file."),
    apply: bool = typer.Option(False, "--apply", help="Do it. Without this, nothing is changed."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation (needed when not at a keyboard)."),
    settle: float = SETTLE_OPTION,
) -> None:
    """Restore markdown files in a git repository to a snapshot.

    The restore is one new commit. History is never rewritten, anything not
    already in git is backed up first, and the rollback can itself be undone.
    """
    ledger = _open_ledger(db)
    snapshot = ledger.load_snapshot(to)  # fail before doing any work
    adapter, scope, _ = _setup_markdown(path, store, subdir)
    _rollback_command(adapter, scope, Restorer(adapter), ledger, snapshot, only=list(only or []), remove_added=remove_added, full=full,
                      apply=apply, yes=yes, settle=settle,
                      undo_command="memdebug rollback markdown --path <the same folder> --to {id} --apply",
                      record_command="memdebug sync markdown --path ...")


def _rollback_command(adapter: MemoryAdapter, scope: dict[str, str], restorer: Restoring, ledger: Ledger, snapshot: Snapshot, *, only: list[str],
                      remove_added: bool, full: bool, apply: bool, yes: bool, settle: float, undo_command: str, record_command: str) -> None:
    """Plan, show, confirm and carry out a rollback. Shared by every store type, so they all behave the same way."""
    chosen = list(only)
    plan = restorer.plan(snapshot, only=chosen, remove_added=remove_added)
    label = f" ({safe_text(snapshot.info.label, 60)})" if snapshot.info.label else ""
    _print_plan(plan, label, full)
    if plan.blockers:
        raise typer.Exit(1)
    if not apply:
        echo("\nDry run: nothing was changed. Add --apply to do it.")
        return
    if not plan.items:
        return
    if not yes:
        if not (os.isatty(0) and os.isatty(1)):
            raise MemdebugError("not at a keyboard: pass --yes to confirm, or run it from a terminal")
        typed = typer.prompt(f"\nType {snapshot.info.id} to confirm", default="", show_default=False)
        if typed.strip() != snapshot.info.id:
            echo("Cancelled. Nothing was changed.")
            raise typer.Exit(1)

    result = run_rollback(adapter, ledger, scope, restorer, snapshot, plan, only=chosen, remove_added=remove_added, settle=settle,
                          warn=lambda text: echo(f"warning: {text}", err=True))
    outcome = result.outcome
    if result.entry is None:
        echo(f"The files were restored, but recording it in the ledger failed: {result.record_error}", err=True)
        echo(f"Run '{record_command}' to record the changes. Undo point: snapshot {result.before.id}.", err=True)
        raise typer.Exit(3)
    echo(f"\nRestored {len(outcome.items)} file(s) to {snapshot.info.id}. Recorded as {result.entry.event.memory_id}.")
    if outcome.commit:
        echo(f"New commit {outcome.commit[:12]} on {safe_text((outcome.branch or '').removeprefix('refs/heads/'), 60)} "
             f"(it was at {(outcome.previous_head or '')[:12]}).")
    if outcome.backup_path:
        echo(f"What was replaced or removed was saved first, byte for byte: {safe_text(outcome.backup_path, 200)}")
        echo("  Get a file back by copying it out of that folder's 'files' folder.")
    elif outcome.backup_ref:
        echo(f"Content that git did not have was saved first: {outcome.backup_ref}")
        echo("  Get a file back with: git checkout <that name> -- <file>")
    echo(f"To undo this rollback: {undo_command.format(id=result.before.id)}")
    echo("Now restart your agent's session: a running session keeps the memory it already loaded.")
    echo(f"Ledger head: {ledger.head()}")


@rollback_app.command("store")
@guarded
def rollback_store(
    name: str = typer.Argument(..., help="The name of a watched store (see 'memdebug stores')."),
    to: str = typer.Option(..., "--to", help="The snapshot to go back to (for example s1; 'memdebug snapshot list' shows them)."),
    db: Path = DB_OPTION,
    only: Optional[list[str]] = typer.Option(None, "--only", help="Restore only this file (repeat for several)."),
    remove_added: bool = typer.Option(False, "--remove-added", help="Also remove files added since the snapshot (they are saved first)."),
    full: bool = typer.Option(False, "--full", help="Show the line changes for each file."),
    apply: bool = typer.Option(False, "--apply", help="Do it. Without this, nothing is changed."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation (needed when not at a keyboard)."),
    settle: float = SETTLE_OPTION,
) -> None:
    """Roll back a watched store, by name, to a snapshot.

    Markdown notes in git are restored with a new commit; a plain folder of
    notes is changed in place, with whatever is replaced saved first. A folder
    is rebuilt from the snapshot's text, which does not keep line endings
    exactly. Open WebUI and Mem0 keep their memory in databases that memdebug
    only ever reads, so they cannot be rolled back.
    """
    cfg = _watched_store(name, db)
    if cfg.kind not in ("markdown", "folder"):
        raise MemdebugError(f"{KIND_NAMES[cfg.kind]} cannot be rolled back: memdebug only ever reads it. Review or change it in the app itself.")
    ledger = _open_ledger(db)
    snapshot = ledger.load_snapshot(to)  # fail before doing any work
    opened = open_store(cfg, refresh=False)
    restorer: Restoring
    if cfg.kind == "markdown":
        restorer = Restorer(cast(MarkdownGitAdapter, opened.adapter))
    else:
        restorer = FolderRestorer(cast(FolderAdapter, opened.adapter), backup_root_for(db or default_ledger_path()))
    _rollback_command(opened.adapter, opened.scope, restorer, ledger, snapshot, only=list(only or []), remove_added=remove_added, full=full,
                      apply=apply, yes=yes, settle=settle, undo_command=f"memdebug rollback store {cfg.name} --to {{id}} --apply",
                      record_command="memdebug check")


# -- backups ------------------------------------------------------------------------------------------------

backups_app = typer.Typer(
    help="List and remove the backups that rollbacks of plain folders leave next to the ledger. Nothing is removed without --apply.",
    no_args_is_help=True,
)
app.add_typer(backups_app, name="backups")


def _size_text(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # unreachable; satisfies the type checker


def _age_text(created: datetime, now: datetime) -> str:
    days = (now - created).days
    return "today" if days < 1 else f"{days} day(s) ago"


def _print_backups(backups: list[Backup], now: datetime) -> None:
    echo(f"  {'store':<20} {'made (UTC)':<17} {'files':>5} {'size':>9}  age")
    for b in backups:
        flag = "  (holds a link: never removed)" if b.has_link else ""
        echo(f"  {safe_text(b.store, 20):<20} {b.created:%Y-%m-%d %H:%M}   {b.files:>5} {_size_text(b.size):>9}  {_age_text(b.created, now)}{flag}")


@backups_app.command("list")
@guarded
def backups_list(db: Path = DB_OPTION) -> None:
    """Show the backups of plain-folder rollbacks, newest first, with their size and age. Changes nothing."""
    root = backup_root_for(db or default_ledger_path())
    listing = list_backups(root)
    if not listing.backups:
        echo(f"No backups in {safe_text(root, 200)}.")
    else:
        _print_backups(listing.backups, datetime.now(timezone.utc))
        echo(f"\n{len(listing.backups)} backup(s), {_size_text(sum(b.size for b in listing.backups))}, in {safe_text(root, 200)}")
    if listing.ignored:
        echo(f"{listing.ignored} other entr{'y' if listing.ignored == 1 else 'ies'} in that folder are not backups and are left alone.")
    echo("These copies hold your memory text. Git rollbacks keep theirs inside the repository, under refs/memdebug/backups/; this does not touch those.")


@backups_app.command("clean")
@guarded
def backups_clean(
    db: Path = DB_OPTION,
    older_than: Optional[int] = typer.Option(None, "--older-than", min=0, help="Only backups made more than this many days ago."),
    keep: Optional[int] = typer.Option(None, "--keep", min=0, help="Never remove the newest N backups of each store."),
    store: Optional[str] = typer.Option(None, "--store", help="Only the backups of this store."),
    everything: bool = typer.Option(False, "--all", help="Every backup (of the store, if --store is given). Not combined with the other conditions."),
    apply: bool = typer.Option(False, "--apply", help="Do it. Without this, nothing is removed."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation (needed when not at a keyboard)."),
) -> None:
    """Remove old backups of plain-folder rollbacks, once you are sure you no longer need them.

    You must say which: --older-than DAYS, --keep N, or --all. A backup that meets every condition you give is chosen. Shows the list
    first; nothing is removed without --apply. A removed backup cannot be brought back.
    """
    if older_than is None and keep is None and not everything:
        raise MemdebugError("say which backups: --older-than DAYS, --keep N (per store), or --all. 'memdebug backups list' shows what there is")
    root = backup_root_for(db or default_ledger_path())
    now = datetime.now(timezone.utc)
    listing = list_backups(root)
    chosen = select(listing.backups, now=now, older_than_days=older_than, keep=keep, store=store, everything=everything)
    removable = [b for b in chosen if not b.has_link]
    if not chosen:
        echo("Nothing matches. Nothing was removed.")
        return
    echo("Would remove:" if not apply else "Removing:")
    _print_backups(chosen, now)
    skipped = len(chosen) - len(removable)
    if skipped:
        echo(f"\n{skipped} of them hold a link and are left alone.")
    echo(f"\n{len(removable)} backup(s), {_size_text(sum(b.size for b in removable))}. A removed backup cannot be brought back.")
    if not apply:
        echo("\nDry run: nothing was removed. Add --apply to do it.")
        return
    if not removable:
        return
    if not yes:
        if not (os.isatty(0) and os.isatty(1)):
            raise MemdebugError("not at a keyboard: pass --yes to confirm, or run it from a terminal")
        typer.confirm(f"\nRemove {len(removable)} backup(s)?", abort=True)
    done = 0
    failures = 0
    for b in removable:
        try:
            remove_backup(root, b)
            done += 1
        except BackupError as exc:
            failures += 1
            echo(f"error: {safe_text(b.store, 20)}/{safe_text(b.name, 30)}: {safe_text(exc, 300)}", err=True)
    echo(f"\nRemoved {done} backup(s).")
    if failures:
        raise typer.Exit(1)


@app.command("demo")
@guarded
def demo_command(
    folder: Optional[Path] = typer.Option(None, "--dir", help="Work in this new or empty folder (it is kept). Default: a temporary folder."),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary folder afterwards."),
    serve_viewer: bool = typer.Option(False, "--serve", help="Afterwards, open the demo ledger in the browser viewer."),
) -> None:
    """Try memdebug in about a minute.

    A made-up agent memory, a made-up attack, and what you would see. It works
    in a throwaway folder and never reads or changes anything of yours.
    """
    from .demo import prepare_folder, remove_folder, run_demo

    path, temporary = prepare_folder(folder)
    try:
        result = run_demo(path, echo)
        if serve_viewer:
            import logging

            from .viewer.server import serve

            logging.basicConfig(level=logging.INFO, format="%(message)s")

            def announce(url: str) -> None:
                echo("\nThis is the demo ledger in the read-only viewer (this computer only). Open this link (it contains a secret):")
                echo(f"  {url}")
                echo("Look at: Overview, Timeline (the amber entry and the ROLLBACK), Snapshots, Integrity. Ctrl+C to stop.")

            serve(result.ledger_path, 0, announce, True)
    finally:
        if temporary and not keep:
            remove_folder(path)
    if temporary and keep or not temporary:
        echo(f"\nThe demo folder was kept: {path}")
    else:
        echo("\nThe throwaway folder was removed.")
    echo("Next: see which agents keep memory on this computer and choose what to watch:  memdebug setup")
    echo("      (it asks before watching anything; 'memdebug agents' only lists what it finds)")


# -- registered stores: add, stores, remove, check, status, watch, setup ---------------------------------------------------

def _config_path(db: Optional[Path]) -> Path:
    """The list of watched stores lives next to the ledger it belongs to."""
    return (db or default_ledger_path()).with_name("stores.json")


def _watched_store(name: str, db: Optional[Path]) -> StoreConfig:
    cfg = load_registry(_config_path(db)).get(name)
    if cfg is None:
        raise MemdebugError(f"there is no watched store named {safe_text(name, 40)}; 'memdebug stores' lists them")
    return cfg


def _guess_name(path: Path, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (path.stem if path.is_file() else path.name).lower()).strip("-")[:30] or "memory"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _copies_dir(db: Optional[Path]) -> Path:
    """Where memdebug keeps the copies it takes of databases that live inside containers."""
    return _config_path(db).parent / "copies"


def _register(registry: Registry, path: Path, *, name: Optional[str], kind: Optional[str], user_id: Optional[str], agent_id: Optional[str],
              run_id: Optional[str], subdir: Optional[str], docker: Optional[str] = None, files: Optional[str] = None) -> StoreConfig:
    """Check that a store can be opened, then add it to the registry (not yet saved)."""
    resolved = longpath.resolve(path.expanduser())
    chosen_kind = kind or detect_kind(resolved)
    if chosen_kind not in KINDS:
        raise MemdebugError("the type must be one of: " + ", ".join(KINDS))
    same = next((s for s in registry.stores if (s.path == str(resolved) or (docker and s.docker == docker)) and s.subdir == subdir and (user_id is None or s.user_id == user_id) and s.files == files), None)
    if same is not None:
        raise MemdebugError(f"that is already watched as {safe_text(same.name, 40)}")
    chosen_name = name or _guess_name(resolved, {s.name for s in registry.stores})
    if not valid_name(chosen_name):
        raise MemdebugError("a store name must be 1 to 40 lowercase letters, digits, dots, dashes or underscores")
    cfg = StoreConfig(name=chosen_name, kind=chosen_kind, path=str(resolved), subdir=subdir, user_id=user_id, agent_id=agent_id, run_id=run_id, docker=docker, files=files)
    opened = open_store(cfg)  # fails clearly if this is not something memdebug can read
    if cfg.kind == "openwebui" and cfg.user_id is None and isinstance(opened.adapter, OpenWebUIAdapter):
        # Whose memories to read is worked out once, now, and remembered: re-deriving it on every look would break a store that
        # later has no memories, or a second user.
        cfg = replace(cfg, user_id=opened.adapter.user)
    registry.add(cfg)
    return cfg


def _say_hints(cfg: StoreConfig, *, refresh: bool = True) -> None:
    """Wording already in the store that is worth a second look, said right away: a planted phrase may predate memdebug."""
    try:
        found = store_hints(cfg, refresh=refresh)
    except MemdebugError:
        return
    for label, hint in found[:3]:
        echo(f"  worth a second look: {safe_text(label, 90)}: {safe_text(hint.message, 120)}")
    if len(found) > 3:
        echo(f"  ... and {len(found) - 3} more. These are guesses from wording; 'memdebug serve' shows each one.")


@app.command("add")
@guarded
def add_command(
    path: Optional[Path] = typer.Argument(None, help="A folder of notes, a git repository, or a database (a copy of Open WebUI's webui.db, Mem0's history.db). Not needed with --docker."),
    docker: Optional[str] = typer.Option(None, "--docker", help="Open WebUI running in Docker: the container's name (see 'docker ps'). memdebug copies its database itself."),
    name: Optional[str] = typer.Option(None, "--name", help="A short name for it (default: taken from the path)."),
    kind: Optional[str] = typer.Option(None, "--type", help="markdown, folder, openwebui or mem0 (default: worked out from the path)."),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="Whose memories (needed for Mem0; for Open WebUI only if several users)."),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Mem0 only."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Mem0 only."),
    subdir: Optional[str] = typer.Option(None, "--subdir", help="Only read this folder inside it."),
    files: Optional[str] = typer.Option(None, "--files", help="A folder store only: watch just these markdown files in it (comma-separated), nothing else in the folder."),
    baseline_now: bool = typer.Option(True, "--baseline/--no-baseline", help="Save a first snapshot right away (recommended)."),
    db: Path = DB_OPTION,
) -> None:
    """Tell memdebug to watch a memory store, once. After that, 'memdebug check' looks at it and 'memdebug watch' keeps looking."""
    ledger = _open_ledger(db)
    config = _config_path(db)
    registry = load_registry(config)
    if docker is not None:
        if path is not None:
            raise MemdebugError("give either a path or --docker, not both")
        if not valid_container(docker):
            raise MemdebugError("that is not a usable container name; see 'docker ps'")
        kind = "openwebui"
        name = name or _guess_name(Path(docker), {s.name for s in registry.stores})
        path = _copies_dir(db) / name / "webui.db"
    elif path is None:
        raise MemdebugError("say what to watch: a path, or --docker NAME for Open WebUI in Docker")
    cfg = _register(registry, path, name=name, kind=kind, user_id=user_id, agent_id=agent_id, run_id=run_id, subdir=subdir, docker=docker, files=files)
    save_registry(registry, config)
    echo(f"Added {safe_text(cfg.name, 40)}: {KIND_NAMES[cfg.kind]}.")
    if baseline_now:
        echo(f"Saved a first snapshot, {baseline(cfg, ledger, refresh=False)}, of what it holds now. Later changes are compared with it.")
        _say_hints(cfg, refresh=False)
    echo("Next: 'memdebug check' looks for changes, 'memdebug watch' keeps looking, 'memdebug serve' shows them in the browser.")


@app.command("stores")
@guarded
def stores_command(db: Path = DB_OPTION) -> None:
    """List the stores memdebug watches."""
    registry = load_registry(_config_path(db))
    if not registry.stores:
        echo("No stores yet. Run 'memdebug setup' for a guided start, or 'memdebug add <path>'.")
        return
    for store in registry.stores:
        echo(f"{safe_text(store.name, 40)}: {KIND_NAMES[store.kind]}: {safe_text(store.path, 200)}")
    if registry.witness:
        echo(f"witness file: {safe_text(registry.witness, 200)}")


@app.command("remove")
@guarded
def remove_command(name: str = typer.Argument(..., help="The store's name."), db: Path = DB_OPTION) -> None:
    """Stop watching a store. What was already recorded about it stays in the ledger."""
    config = _config_path(db)
    registry = load_registry(config)
    registry.remove(name)
    save_registry(registry, config)
    echo(f"No longer watching {safe_text(name, 40)}. Its history stays in the ledger.")


@app.command("check")
@guarded
def check_command(
    strict: bool = typer.Option(False, "--strict", help="Also exit with code 1 when changed wording looks worth a second look (for CI)."),
    settle: float = SETTLE_OPTION,
    db: Path = DB_OPTION,
) -> NoReturn:
    """Look at every watched store once: what changed since last time, and is anything wrong? Exit code 1 means something needs a look, or a store could not be read in full."""
    registry = load_registry(_config_path(db))
    if not registry.stores:
        echo("Nothing to check yet. Run 'memdebug setup' for a guided start, or 'memdebug add <path>'.")
        raise typer.Exit(2)
    ledger = _open_ledger(db)
    echo(f"Checking {len(registry.stores)} store(s)...")
    summary = check_all(registry, ledger, settle=settle, ledger_path=db or default_ledger_path())
    for line in summary_lines(summary):
        echo(line)
    for note in [w for r in summary.results for w in r.warnings][:5] + ([summary.witness_warning] if summary.witness_warning else []):
        echo(f"  warning: {safe_text(note, 300)}", err=True)
    if summary.incomplete:
        echo("  Some notes could not be read, so this is not a clean bill of health for the stores marked above. The warnings say why.")
    if summary.attention or summary.hinted:
        echo("  Look closer: 'memdebug serve' shows exactly what changed. 'memdebug rollback store <name> --to <snapshot>' can put markdown or folder notes back.")
    raise typer.Exit(summary.exit_code_for(strict))


@app.command("status")
@guarded
def status_command(db: Path = DB_OPTION) -> None:
    """Where things stand, without looking for new changes or writing anything."""
    registry = load_registry(_config_path(db))
    if not registry.stores:
        echo("No stores yet. Run 'memdebug setup' for a guided start, or 'memdebug add <path>'.")
        return
    ledger = _open_ledger(db)
    echo(f"{len(registry.stores)} store(s) watched; ledger {'intact' if ledger.verify().ok else 'HAS PROBLEMS'}.")
    for line in status_lines(registry, ledger):
        echo(line)


@app.command("watch")
@guarded
def watch_command(
    every: float = typer.Option(60.0, "--every", min=5.0, max=86400.0, help="Seconds between looks."),
    once: bool = typer.Option(False, "--once", help="Look once and stop (like 'check', but prints only what is new)."),
    bell: bool = typer.Option(True, "--bell/--no-bell", help="Ring the terminal bell when something needs a look."),
    settle: float = SETTLE_OPTION,
    db: Path = DB_OPTION,
) -> None:
    """Keep looking at every watched store and say so when something changes. Stop with Ctrl+C."""
    registry = load_registry(_config_path(db))
    if not registry.stores:
        echo("Nothing to watch yet. Run 'memdebug setup' for a guided start, or 'memdebug add <path>'.")
        raise typer.Exit(2)
    ledger = _open_ledger(db)
    try:
        watch(registry, ledger, every=every, say=echo, ring=(lambda: typer.echo("\a", nl=False)) if bell else (lambda: None),
              cycles=1 if once else None, settle=settle, ledger_path=db or default_ledger_path())
    except KeyboardInterrupt:
        echo("\nStopped.")


@app.command("agents")
@guarded
def agents_command() -> None:
    """Which AI agents memdebug can see on this computer, and what each keeps. Only looks at folder names; changes nothing."""
    for line in agent_lines(scan_agents(), [c.docker for c in discover_docker(Path(tempfile.gettempdir())) if c.docker]):
        echo(line)
    echo("\n'memdebug setup' offers to watch what it found.")


@app.command("setup")
@guarded
def setup_command(
    yes: bool = typer.Option(False, "--yes", help="Accept everything that is found and ask nothing."),
    db: Path = DB_OPTION,
) -> None:
    """A guided start: find the memory your agents keep on this computer, save a first snapshot of each, and optionally set up a witness."""
    ledger = _open_ledger(db)
    config = _config_path(db)
    registry = load_registry(config)
    interactive = not yes
    echo("memdebug setup")
    echo("It looks for memory on this computer, tells you what it found, and saves a first snapshot of each store you keep.")
    echo("Nothing is changed in your memory; memdebug only reads it.\n")
    added: list[StoreConfig] = []
    watching = {(s.path, s.files) for s in registry.stores}
    watched_containers = {s.docker for s in registry.stores if s.docker}
    agents = scan_agents()
    containers = [c for c in discover_docker(_copies_dir(db)) if c.docker not in watched_containers]
    echo("Agents on this computer:")
    for line in agent_lines(agents, [c.docker for c in containers if c.docker]):
        echo(line)
    found = [c for item in agents for c in item.candidates if (str(c.path.resolve()), ",".join(c.files) if c.files else None) not in watching] + containers
    if found:
        echo("\nWhat memdebug can watch:")
    for c in found:
        echo(f"  {safe_text(c.name, 40)}: {c.why}  ({safe_text(c.path, 120)})")
        if yes or typer.confirm("  Watch it?", default=True):
            try:
                added.append(_register(registry, c.path, name=c.name, kind=c.kind, user_id=None, agent_id=None, run_id=None, subdir=None, docker=c.docker,
                                       files=",".join(c.files) if c.files else None))
            except MemdebugError as exc:
                echo(f"  Could not set that up: {safe_text(exc, 200)}")
    if not found:
        echo("Nothing was found automatically (that is normal if your agent keeps its memory somewhere else).")
    while interactive:
        raw = typer.prompt("\nAnother folder of notes, git repository or database to watch? Enter its path, or just press Enter to continue",
                           default="", show_default=False).strip().strip('"')
        if not raw:
            break
        try:
            path = Path(raw)
            guessed = detect_kind(path.expanduser().resolve())
            user = typer.prompt("  Mem0 needs a user id", default="") if guessed == "mem0" else None
            added.append(_register(registry, path, name=None, kind=guessed, user_id=user or None, agent_id=None, run_id=None, subdir=None))
            echo(f"  Will watch it as {KIND_NAMES[guessed]}.")
        except MemdebugError as exc:
            echo(f"  Could not use that: {safe_text(exc, 200)}")
    for cfg in added:
        echo(f"\nSaving a first snapshot of {safe_text(cfg.name, 40)}...")
        try:
            echo(f"  Done: {baseline(cfg, ledger, refresh=False)}")
            _say_hints(cfg, refresh=False)
        except MemdebugError as exc:
            echo(f"  Could not read it: {safe_text(exc, 200)}")
    if interactive and registry.stores and not registry.witness:
        echo("\nA witness is a second copy of the ledger's fingerprint, kept somewhere else, so a rewritten or cut-short ledger is noticed.")
        folder = typer.prompt("Folder for it, ideally on another drive or a synced folder (press Enter to skip)", default="", show_default=False).strip().strip('"')
        if folder:
            target = Path(folder).expanduser() / "memdebug-witness.txt"
            try:
                append_witness(ledger, target)
                registry.witness = str(target.resolve())
                echo(f"  Witness set up: {safe_text(target, 160)}")
                if same_disk(db or default_ledger_path(), target):
                    echo(f"  warning: {SAME_DISK_WARNING}")
            except MemdebugError as exc:
                echo(f"  Could not set up the witness: {safe_text(exc, 200)}")
    save_registry(registry, config)
    if not registry.stores:
        echo("\nNo stores yet. Add one later with: memdebug add <path>")
        return
    echo(f"\nWatching {len(registry.stores)} store(s). From now on:")
    echo("  memdebug check    look for changes once")
    echo("  memdebug watch    keep looking, and say when something changes")
    echo("  memdebug serve    see it all in your browser")


@app.command("serve")
@guarded
def serve_command(
    db: Path = DB_OPTION,
    port: int = typer.Option(8765, min=0, max=65535, help="Port on this computer (0 picks a free one)."),
    open_browser: bool = typer.Option(False, "--open", help="Open the link in your browser (it contains the secret)."),
) -> None:
    """Start a read-only viewer on this computer only (127.0.0.1). Open the link it prints."""
    import logging

    from .viewer.server import serve

    path = db if db is not None else default_ledger_path()
    if not path.is_file():
        raise MemdebugError("there is no ledger at that path yet; run 'memdebug sync ...' first")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def announce(url: str) -> None:
        echo("Read-only viewer running on this computer only. Open this link (it contains a secret):")
        echo(f"  {url}")
        echo("Press Ctrl+C to stop.")

    try:
        serve(path, port, announce, open_browser)
    except OSError as exc:
        raise MemdebugError(f"cannot start the viewer on port {port}: {exc.strerror}. Try --port 0.") from exc
