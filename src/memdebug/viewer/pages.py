"""The viewer's pages. Every page is built with el(), so untrusted text is escaped by construction.

Nothing here reads request input directly: the server validates it first, and the values that reach
a page are ids matched against strict patterns, small integers, and members of fixed lists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..agents import CLOUD_NOTE, FoundAgent
from ..backends import HISTORYLESS, is_opaque_id, short_id
from ..describe import describe_event, rollback_details
from ..diff import DIFF_HEADERS, Diff, diff_snapshots, line_diff
from ..errors import SnapshotError
from ..hints import new_hints
from ..ledger import Ledger, VerifyResult
from ..models import LedgerEntry, MemoryEvent, SnapshotInfo, Trust
from ..stores import valid_name
from .html import Markup, block, el, inline, raw_doctype
from .redline import change_snippet, redline

PAGE_SIZE = 40
SNAPSHOT_PAGE_SIZE = 100
MAX_DIFF_CHANGES = 200

OP_CLASS = {
    "ADD": "op-add", "UPDATE": "op-update", "DELETE": "op-delete", "EXTERNAL": "op-external",
    "SNAPSHOT": "op-snapshot", "SNAP_DEL": "op-snap-del", "ROLLBACK": "op-rollback",
}
NODE_CLASS = {  # the shape of an entry's node on the chain
    "ADD": "n-add", "UPDATE": "n-update", "DELETE": "n-delete", "EXTERNAL": "n-external",
    "SNAPSHOT": "n-snapshot", "SNAP_DEL": "n-snap-del", "ROLLBACK": "n-rollback",
}
OP_LABEL = {"EXTERNAL": "OUTSIDE HISTORY", "SNAP_DEL": "SNAPSHOT DELETED"}
FILTERS = [  # (label, op, trust)
    ("All", None, None), ("Added", "ADD", None), ("Changed", "UPDATE", None), ("Deleted", "DELETE", None),
    ("Outside the history", "EXTERNAL", None), ("Snapshots", "SNAPSHOT", None), ("Rollbacks", "ROLLBACK", None),
    ("Untrusted source", None, "untrusted"),
]
NAV = [("overview", "Overview", "/"), ("timeline", "Timeline", "/timeline"), ("snapshots", "Snapshots", "/snapshots"),
       ("compare", "Compare", "/diff"), ("agents", "Agents", "/agents"), ("integrity", "Integrity", "/integrity")]


@dataclass(frozen=True)
class Page:
    """A finished response.

    Attributes:
        status: The HTTP status code.
        html: The complete HTML document.
    """

    status: int
    html: str


@dataclass(frozen=True)
class Context:
    """What every page needs to know about the request that is not part of the ledger.

    Attributes:
        ledger_name: The ledger's file name, shown in the page header.
        theme: "auto", "light" or "dark".
        here: The address of the page being shown.
    """

    ledger_name: str
    theme: str = "auto"  # "auto" follows the computer's setting; "light" and "dark" are the person's choice
    here: str = "/"      # the page being shown, so the theme switch can return to it


THEMES = (("auto", "Auto"), ("light", "Light"), ("dark", "Dark"))


def url(path: str, **params) -> str:
    """A path followed by a query string built from the params that are not None, sorted by name."""
    query = urlencode([(k, v) for k, v in sorted(params.items()) if v is not None])
    return f"{path}?{query}" if query else path


def when(moment: datetime) -> str:
    """A moment to the second in UTC, such as "2026-10-09 14:03:07 UTC"."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def stamp(moment: datetime) -> str:
    """A short, quiet time for lists; the header says all times are UTC."""
    utc = moment.astimezone(timezone.utc)
    return f"{utc.day} {utc:%b %Y}, {utc:%H:%M}"


def plural(count: int, one: str, many: str) -> str:
    """The count followed by `one` when it is 1, otherwise by `many`."""
    return f"{count} {one if count == 1 else many}"


def short(digest: str) -> str:
    """The first 12 characters of a hash."""
    return digest[:12]


PUT_BACK_KINDS = ("folder", "markdown-git")  # the store types memdebug can write back; it only ever reads the others


def earlier_snapshot(snapshots, backend: str, scope: dict, seq: int) -> SnapshotInfo | None:
    """The latest snapshot of this store taken before ledger entry `seq`.

    That is the saved state a person would most likely want back. A snapshot belongs to the store when its
    backend and scope both match; None is returned when there is no such snapshot.
    """
    found = [i for i in snapshots if i.backend == backend and i.scope == scope and i.ledger_seq < seq]
    return max(found, key=lambda i: (i.ledger_seq, int(i.id[1:])), default=None)


def rollback_command(backend: str, scope: dict, snapshot_id: str) -> str | None:
    """The command that puts a store back to a snapshot, or None when it cannot be given safely.

    The store's name comes from the ledger, which anyone could edit, so it is only put into a command when it
    passes the same strict rule as a watched store's name: nothing a shell treats specially, so pasting the
    command can never do anything else. None is also returned for a kind of store that memdebug cannot write.
    """
    name = scope.get("store")
    if backend not in PUT_BACK_KINDS or not isinstance(name, str) or not valid_name(name):
        return None
    return f"memdebug rollback store {name} --to {snapshot_id}"


def putback(backend: str, scope: dict, snapshot_id: str, *, heading: str = "Put it back", lead: str | None = None) -> Markup:
    """How to put a store back to a snapshot, as a section of the page.

    The viewer itself never changes anything: this is text to paste into a terminal, where memdebug shows what
    would change first and asks before doing it. For a kind of store memdebug only reads, the section says so
    instead. When the store's name cannot be put into a command safely, the command shows `<name>` in its place.

    Args:
        backend: The kind of store, such as "folder" or "markdown-git".
        scope: The store's scope. Its "store" entry, when valid, names the store in the command.
        snapshot_id: The snapshot to put the store back to.
        heading: The section's heading.
        lead: Replaces the default introductory sentence.
    """
    if backend not in PUT_BACK_KINDS:
        return el("section", el("h2", heading), el(
            "p", "memdebug only ever reads this kind of store, so it cannot put it back. Open Compare to see exactly what changed, and "
                 "change it in the app itself.", class_="why"), class_="putback")
    command = rollback_command(backend, scope, snapshot_id)
    shown = f"{command}\n{command} --apply" if command else \
        f"memdebug rollback store <name> --to {snapshot_id}\nmemdebug rollback store <name> --to {snapshot_id} --apply"
    how = ("Files come back byte for byte from git, as one new commit; anything git did not hold is saved first."
           if backend == "markdown-git" else
           "A folder is rebuilt from the snapshot's text, so line endings may differ (LF, or CRLF if the file had CRLF throughout). Everything it "
           "replaces is saved byte for byte in a private backup folder first.")
    parts: list[Markup] = [
        el("h2", heading),
        el("p", lead or "This page cannot change anything. To put the store back to this snapshot, run this in a terminal. The first line only "
                        "shows what would change; the second does it, after asking you to confirm.", class_="why"),
        el("pre", block(shown)),
        el("p", how + " The rollback is recorded here and can itself be undone.", class_="sub"),
    ]
    if not command:
        parts.append(el("p", "Replace <name> with the store's name from 'memdebug stores'.", class_="sub"))
    return el("section", *parts, class_="putback")


def scope_text(scope: dict) -> str:
    """A store's scope as "key=value" pairs sorted by key, or "-" when it is empty."""
    return ", ".join(f"{k}={v}" for k, v in sorted(scope.items())) or "-"


def document(ctx: Context, title: str, active: str, content: Markup) -> str:
    """The whole HTML page around `content`: head, header with navigation and theme switch, and footer.

    Args:
        ctx: Supplies the ledger name, the theme and the address the theme switch returns to.
        title: The page title; " - memdebug" is added to it.
        active: The key in `NAV` of the section to mark as current, or "" for none.
        content: The page's own content, placed in <main>.

    Returns:
        The document text, starting with the doctype.
    """
    tabs = el("nav", *[
        el("a", label, href=path, aria_current="page" if key == active else None) for key, label, path in NAV
    ], class_="tabs", aria_label="Sections")
    head = el(
        "head",
        el("meta", charset="utf-8"),
        el("meta", name="viewport", content="width=device-width, initial-scale=1"),
        el("meta", name="referrer", content="no-referrer"),
        el("title", f"{title} - memdebug"),
        el("link", rel="stylesheet", href="/style.css"),
    )
    switch = el("div", *[
        el("a", label, href=url("/theme", mode=key, next=ctx.here), aria_current="true" if key == ctx.theme else None)
        for key, label in THEMES
    ], class_="theme", role="group", aria_label="Colour theme")
    top = el("header", el("span", "memdebug", class_="brand"), el("span", inline(ctx.ledger_name, 60), class_="where"),
             el("span", "Read-only, times in UTC", class_="ro"), tabs, switch, class_="top")
    foot = el("footer", "This page only reads the ledger. It cannot change a memory store or the ledger.")
    body = el("body", top, el("main", content), foot)
    theme_class = f"theme-{ctx.theme}" if ctx.theme in ("light", "dark") else None
    return str(raw_doctype()) + str(el("html", head, body, lang="en", class_=theme_class))


def badge(event: MemoryEvent) -> Markup:
    """The coloured label naming what kind of event this is, such as ADD or OUTSIDE HISTORY."""
    return el("span", OP_LABEL.get(event.op.value, event.op.value), class_=f"badge {OP_CLASS[event.op.value]}")


def notice(text: str, kind: str = "") -> Markup:
    """A status message box. `kind` adds a style class: the pages use "ok" and "bad"."""
    return el("div", text, class_=f"notice {kind}".strip(), role="status")


def error_page(ctx: Context, status: int, title: str, message: str) -> Page:
    """A page with a heading and an error message, carrying the given HTTP status.

    No section in the navigation is marked as current.
    """
    content = el("h1", title), notice(message, "bad")
    return Page(status, document(ctx, title, "", Markup("".join(content))))


def diff_lines(lines: list[str]) -> Markup:
    """The lines of a unified diff as a <pre>, with added, removed and hunk lines styled differently.

    Only the two file-header lines at the very top are skipped. Any other
    line is memory text and is shown, even if it begins with "+++" or "---".
    """
    spans: list[Markup] = []
    for number, line in enumerate(lines):
        if number < len(DIFF_HEADERS) and line == DIFF_HEADERS[number]:
            continue
        kind = "add" if line.startswith("+") else "del" if line.startswith("-") else "hunk"
        spans.append(el("span", block(line, 2000), class_=kind))
    return el("pre", *spans)


# -- the chain: one entry per recorded event ----------------------------------------------------------------------

_BREAKS = re.compile(r"[ \t\r\n]+")


def _excerpt(event: MemoryEvent, limit: int = 260) -> Markup:
    """The start of a memory on one line.

    Only spaces, tabs and line breaks are folded together; any other control character stays visible, as
    everywhere else in the viewer.
    """
    return inline(_BREAKS.sub(" ", describe_event(event)).strip(), limit)


def event_row(entry: LedgerEntry, href: str, selected: bool, snaps: dict[str, SnapshotInfo] | None = None,
              show_store: bool = False) -> Markup:
    """One entry on the chain. The list item carries the dot's colour and icon; the link carries the content.

    Args:
        entry: The ledger entry to show.
        href: Where the row's link goes.
        selected: Whether the row is the one currently open.
        snaps: Snapshots by id, used to describe snapshot entries.
        show_store: Add the entry's store (backend and scope) to the row.
    """
    event = entry.event
    op = event.op.value
    seq = el("span", inline(entry.id, 12), class_="seq")
    moment = el("time", ("noticed " if event.ts_observed else "") + stamp(event.ts), class_="when")
    if op == "SNAPSHOT":
        sid = event.memory_id.removeprefix("snapshot:")
        info = (snaps or {}).get(sid)
        top = [badge(event), el("strong", sid, class_="what")]
        if info is not None and info.label:
            top.append(el("span", inline(info.label, 100), class_="label"))
        top.append(moment)
        detail: list = []
        if info is not None:
            detail.append(el("span", plural(info.count, "memory", "memories")))
            if not info.complete:
                detail.append(el("span", "may be incomplete", class_="flag"))
        else:
            detail.append(el("span", _excerpt(event)))
        body = [el("div", *top, class_="top"), el("div", *detail, class_="meta")]
    elif op == "ROLLBACK":
        details = rollback_details(event)
        top = [badge(event), el("strong", inline(event.memory_id.removeprefix("rollback:"), 40), class_="what")]
        if details is not None:
            top.append(el("span", f"back to {details['target']}", class_="label"))
        top.append(moment)
        summary = plural(details["file_count"], "file", "files") + " changed" if details is not None else "unreadable record"
        body = [el("div", *top, class_="top"), el("div", el("span", summary), class_="meta")]
    elif op == "SNAP_DEL":
        body = [el("div", badge(event), el("strong", inline(event.memory_id.removeprefix("snapshot:"), 40), class_="what"),
                   moment, class_="top"), el("div", el("span", _excerpt(event)), class_="meta")]
    else:
        meta: list = []
        if show_store:
            meta.append(el("span", f"{event.backend} {scope_text(event.scope)}".strip()))
        if event.trust == Trust.UNTRUSTED:
            meta.append(el("span", "came from an untrusted source", class_="flag"))
        if op in ("ADD", "UPDATE", "EXTERNAL") and any(h.severity == "warning" for h in new_hints(event.before, event.after, budget=0.02, max_chars=5000)):
            meta.append(el("span", "worth a second look", class_="flag hint"))
        preview = _excerpt(event)
        if op in ("UPDATE", "EXTERNAL") and event.before is not None and event.after is not None:
            found = change_snippet(event.before, event.after)  # show what changed, not the start of the file
            if found is not None:
                preview = found[0]
                if found[1]:
                    meta.insert(0, el("span", found[1]))
        body = [el("div", badge(event), el("strong", inline(short_id(event.memory_id), 60), class_="what"), moment, class_="top"),
                el("p", preview, class_="mem")]
        if meta:
            body.append(el("div", *meta, class_="meta"))
    link = el("a", *body, class_="row", href=href, aria_current="true" if selected else None)
    return el("li", seq, link, class_=NODE_CLASS[op])


def chain(rows: list[Markup], goes_on: bool = False) -> Markup:
    """The rows as one ordered list, the chain.

    `goes_on` styles the chain as continuing past the last row, because older entries follow.
    """
    return el("ol", *rows, class_="chain goes-on" if goes_on else "chain")


def _stores(entries: list[LedgerEntry]) -> bool:
    return len({(e.event.backend, tuple(sorted(e.event.scope.items()))) for e in entries}) > 1


# -- overview ---------------------------------------------------------------------------------------------------

def _putback_tip(shown: list[LedgerEntry], snaps: dict) -> list[Markup]:
    """For the newest outside-history change in the list: the snapshot taken before it, and the command that would put the store back."""
    newest = next((e for e in shown if e.event.op.value == "EXTERNAL"), None)
    if newest is None:
        return []
    earlier = earlier_snapshot(snaps.values(), newest.event.backend, newest.event.scope, newest.seq)
    command = None if earlier is None else rollback_command(newest.event.backend, newest.event.scope, earlier.id)
    if earlier is None or command is None:
        return []
    return [el("p", "The last snapshot of that store before the newest outside change is ",
               el("a", f"snapshot {earlier.id}", href=f"/snapshot/{earlier.id}"), ". To put the store back, run this in a terminal:", class_="sub"),
            el("pre", block(command))]


def overview(ledger: Ledger, ctx: Context) -> Page:
    """The front page: a verdict, entries that need a look, recent entries, snapshots and the ledger head.

    Reads the ledger only. The verdict counts changes outside the store's history and memories from an untrusted
    source.
    """
    counts = ledger.counts()
    external = counts["by_op"].get("EXTERNAL", 0)
    untrusted = counts["untrusted"]
    snaps = {i.id: i for i in ledger.list_snapshots()}

    if counts["events"] == 0:
        headline = "Nothing is recorded yet."
    elif external:
        headline = f"{plural(external, 'change', 'changes')} happened outside the history."
    elif untrusted:
        headline = f"{plural(untrusted, 'memory', 'memories')} came from an untrusted source."
    else:
        headline = "Nothing needs a look."
    summary = f"{plural(counts['events'], 'event', 'events')} and {plural(counts['snapshots'], 'snapshot', 'snapshots')} are on record."
    if external and untrusted:
        summary += f" {plural(untrusted, 'memory', 'memories')} also came from an untrusted source."
    elif not external and not untrusted and counts["events"]:
        present = ledger.backends()
        quiet = sorted(present & HISTORYLESS)
        if quiet and present == set(quiet):
            summary += (" These stores keep no history of their own, so memdebug records a change when it notices one; it cannot say who "
                        "made it or whether it was expected.")
        elif quiet:
            summary += (f" Changes in the stores that have a history went through it. {', '.join(quiet)} keep no history of their own, so "
                        "there memdebug only records changes when it notices them.")
        else:
            summary += " Every change went through the store's own history."
    parts: list[Markup] = [el("h1", headline, class_="headline"), el("p", summary, class_="sub")]

    flagged: dict[int, LedgerEntry] = {}
    for op, trust in (("EXTERNAL", None), (None, "untrusted")):
        if (op == "EXTERNAL" and external) or (trust and untrusted):
            for e in ledger.events_page(limit=6, op=op, trust=trust):
                flagged[e.seq] = e
    if flagged:
        shown = sorted(flagged.values(), key=lambda e: -e.seq)[:6]
        parts.append(el(
            "section",
            el("h2", "Needs a look"),
            el("p", "These did not come through the store's own history, or came from an untrusted source. "
                    "Open one to see the exact words that changed.", class_="sub"),
            chain([event_row(e, url("/timeline", event=e.id), False, snaps, _stores(shown)) for e in shown]),
            *_putback_tip(shown, snaps),
            class_="attention"))

    latest = ledger.events_page(limit=8)
    parts.append(el("h2", "Recent entries"))
    parts.append(chain([event_row(e, url("/timeline", event=e.id), False, snaps, _stores(latest)) for e in latest])
                 if latest else el("p", "Nothing recorded yet. Run 'memdebug sync ...' first."))
    if latest:
        parts.append(el("p", el("a", "See the whole timeline", href="/timeline")))
    recent_snaps = ledger.list_snapshots()[-3:][::-1]
    parts.append(el("h2", "Snapshots"))
    parts.append(snapshot_table(recent_snaps) if recent_snaps else el("p", "No snapshots yet."))
    parts += [
        el("h2", "Ledger head"),
        el("code", counts["head"], class_="fingerprint"),
        el("small", "A fingerprint of everything recorded so far. Keep a copy somewhere else: it lets you notice if the newest entries "
                    "are ever removed or the ledger is rewritten. 'memdebug witness --file <a file on another drive>' keeps one for you."),
    ]
    return Page(200, document(ctx, "Overview", "overview", Markup("".join(str(p) for p in parts))))


# -- agents --------------------------------------------------------------------------------------------------------

def agents_page(ctx: Context, found: list[FoundAgent]) -> Page:
    """The known agents that appear to be installed, and what memdebug could watch for each.

    Presence is only the existence of a folder: no file is opened. Folder names come from the disk, so they are
    shown as escaped text and never put into a command; the one command given is fixed text.
    """
    parts: list[Markup] = [
        el("h1", "Agents on this computer"),
        el("p", "memdebug checked whether the folders these agents are known to use exist. It opened no file.", class_="sub"),
    ]
    if not found:
        parts.append(notice("None of the agents memdebug knows were found."))
    for item in found:
        places = [el("li", el("strong", inline(c.name, 60)), " - ", inline(c.why, 200), el("br"), el("code", inline(str(c.path), 260)),
                     *([el("br"), el("small", "Only these files are watched: ", inline(", ".join(c.files), 120))] if c.files else []))
                  for c in item.candidates]
        parts.append(el(
            "section", el("h2", inline(item.agent.name, 60)), el("p", inline(item.agent.note, 300), class_="sub"),
            el("ul", *places) if places else el("p", "Nothing to watch yet."), class_="agent"))
    parts += [
        el("h2", "Watch what was found"),
        el("p", "This page cannot change anything. To start watching, run this in a terminal. It shows what it found and asks before it "
                "adds anything.", class_="why"),
        el("pre", block("memdebug setup")),
        el("h2", "Not on this computer"),
        el("p", inline(CLOUD_NOTE, 600), class_="sub"),
    ]
    return Page(200, document(ctx, "Agents", "agents", Markup("".join(str(p) for p in parts))))


# -- timeline ------------------------------------------------------------------------------------------------------

def rollback_section(event: MemoryEvent) -> list[Markup]:
    """The part of an entry's details that describes a rollback.

    It names the snapshot that was restored, lists the files touched and says how to undo the rollback. The
    wording differs for a plain folder and for a git store. A record that cannot be read gives a short message
    that points to the integrity check.
    """
    details = rollback_details(event)
    if details is None:
        return [el("p", "This record could not be read. Run the integrity check.", class_="why")]
    def link(sid, text):
        return el("a", text, href=f"/snapshot/{sid}")

    folder = event.backend == "folder"
    parts: list[Markup] = [
        el("p", "The notes folder was put back to ", link(details["target"], f"snapshot {details['target']}"),
           ". The files were changed in place (there is no git history here, so no commit), and everything replaced or removed was "
           "saved first in a private backup folder. A folder is rebuilt from the snapshot's text, which does not keep line endings exactly.",
           class_="why") if folder else
        el("p", "The memory store was put back to ", link(details["target"], f"snapshot {details['target']}"),
           ". Nothing was rewritten: any change to the files is a new commit, and anything git did not already hold was "
           "saved first.", class_="why"),
        el("h3", "What was done"),
    ]
    shown = details["files"]
    parts.append(el("ul", *[el("li", el("span", inline(f["action"], 20), class_="what-did"), " ", inline(f["path"], 120),
                               el("small", {"git": " from git", "snapshot": " from snapshot text"}.get(f["source"], "")))
                            for f in shown], class_="files"))
    if details["file_count"] > len(shown):
        parts.append(el("p", f"... and {details['file_count'] - len(shown)} more.", class_="why"))
    facts: list[tuple[str, Markup | str]] = []
    if details["before_snapshot"]:
        facts.append(("To undo", el("span", "restore ", link(details["before_snapshot"], f"snapshot {details['before_snapshot']}"),
                                    ", taken just before")))
    if details["after_snapshot"]:
        facts.append(("Afterwards", el("span", link(details["after_snapshot"], f"snapshot {details['after_snapshot']}"),
                                       ", taken just after")))
    if details["commit"]:
        facts.append(("New commit", details["commit"][:12]))
    if details["previous_head"]:
        facts.append(("Branch was at", details["previous_head"][:12]))
    if details["backup"] and folder:
        facts.append(("Saved first in", f"the folder {details['backup'].rsplit('/', 1)[-1]}, inside the 'backups' folder next to your ledger"))
    elif details["backup"]:
        facts.append(("Saved first in", details["backup"]))
    parts.append(el("dl", *[Markup(str(el("dt", k)) + str(el("dd", v if isinstance(v, Markup) else inline(v, 120))))
                            for k, v in facts], class_="facts"))
    return parts


def inspector(entry: LedgerEntry, snapshots: dict | None = None) -> Markup:
    """The detail sheet for one ledger entry: what changed, where it came from, hints and ledger facts.

    Args:
        entry: The entry to show.
        snapshots: Snapshots by id. For a change outside the store's history, the latest one taken before it
            is offered as the state to put the store back to.
    """
    event = entry.event
    op = event.op.value
    source = event.source
    described = source is not None and bool(source.actor_id or source.role or source.note)
    trust_note = {
        Trust.UNTRUSTED: "Came from a tool result (such as an email or web page the agent read), not from the user.",
        Trust.TRUSTED: "Came from something the user wrote.",
        Trust.UNKNOWN: ("Not proven either way. What the app itself records and how close a chat was are shown under Source: a label and a "
                        "timing comparison, not proof." if described else
                        "Where this came from is not known yet (session logs are not connected)."),
    }[event.trust]
    source_text = ""
    if source is not None:
        bits = [f"{k}={v}" for k, v in (("session", source.session_id), ("turn", source.turn), ("kind", source.kind.value)) if v not in (None, "unknown")]
        bits += [x for x in (source.actor_id, source.role) if x]
        source_text = ", ".join(bits) + (f". {source.note}" if source.note else "")
    facts = [
        ("When", when(event.ts) + (" (when it was noticed)" if event.ts_observed else "")),
        ("Backend", event.backend), ("Store", scope_text(event.scope)),
        ("Trust", "" if op in ("SNAPSHOT", "SNAP_DEL", "ROLLBACK") else f"{event.trust.value}. {trust_note}"), ("Source", source_text),
        ("Memory id", event.memory_id if op not in ("SNAPSHOT", "SNAP_DEL", "ROLLBACK") and is_opaque_id(event.memory_id) else ""),
        ("Backend row", event.backend_ref or ""),
        ("Ledger", f"entry {entry.seq}, hash {short(entry.hash)}, previous {short(entry.prev_hash)}"),
    ]
    title = (f"Snapshot {event.memory_id.removeprefix('snapshot:')}" if op in ("SNAPSHOT", "SNAP_DEL")
             else f"Rollback {event.memory_id.removeprefix('rollback:')}" if op == "ROLLBACK" else short_id(event.memory_id))
    main: list[Markup] = [
        el("h2", inline(title, 80)),
        el("p", f"Event {entry.id}, ledger entry {entry.seq}", class_="where2"),
        el("p", badge(event), class_="verdict"),
    ]
    if op == "EXTERNAL":
        main.append(el("p", "Nothing in the store's own history explains this change. If you did not make it, "
                            "treat the new text as untrusted.", class_="why"))
        earlier = earlier_snapshot((snapshots or {}).values(), event.backend, event.scope, entry.seq)
        if earlier is not None:
            main.append(putback(event.backend, event.scope, earlier.id, heading=f"Put the store back to snapshot {earlier.id}",
                                lead=f"Snapshot {earlier.id}, taken before this change, is the most recent saved state of this store. This page "
                                     "cannot change anything; to put the store back to it, run this in a terminal."))
    elif event.trust == Trust.UNTRUSTED:
        main.append(el("p", trust_note, class_="why"))

    if op == "SNAPSHOT":
        sid = event.memory_id.removeprefix("snapshot:")
        main.append(el("p", el("a", f"Open snapshot {sid}", href=f"/snapshot/{sid}")))
    elif op == "SNAP_DEL":
        main.append(el("p", "This snapshot was deleted. Its texts were removed; this record stays.", class_="why"))
    elif op == "ROLLBACK":
        main += rollback_section(event)
    else:
        main.append(el("h3", "What changed"))
        if event.before is not None and event.after is not None:
            marked = redline(event.before, event.after)
            if marked is not None:
                main += [el("p", el("span", "removed", class_="rm"), " ", el("span", "added", class_="ins"), class_="change-key"),
                         marked]
            else:
                main.append(diff_lines(line_diff(event.before, event.after)))
        elif event.after is not None:
            main += [el("p", "Added", class_="change-key"), el("div", block(event.after), class_="quote")]
        elif event.before is not None:
            main += [el("p", "Removed", class_="change-key"), el("div", block(event.before), class_="quote gone")]
        for label, text in (("Full text before", event.before), ("Full text after", event.after)):
            if text is not None:
                main.append(el("details", el("summary", label), el("pre", block(text))))
    if op in ("ADD", "UPDATE", "EXTERNAL"):
        found = new_hints(event.before, event.after)
        if found:
            main.append(el("h3", "Worth a second look"))
            main.append(el("p", "Guesses from the wording of what was added. They miss things and can be wrong: no hint is not a clean bill of health.", class_="why"))
            main.append(el("ul", *[el("li", el("span", inline(h.message, 160)), " ", el("code", inline(h.evidence, 120))) for h in found], class_="hints"))
    visible = [(k, v) for k, v in facts if v]
    side = el("aside", el("h3", "Details"),
              el("dl", *[Markup(str(el("dt", k)) + str(el("dd", inline(v, 300)))) for k, v in visible], class_="facts"),
              class_="sheet-side")
    return el("div", el("div", *main, class_="sheet-main"), side, class_="sheet")


def timeline(ledger: Ledger, ctx: Context, *, op: str | None, trust: str | None, before: int | None,
             event_id: str | None) -> Page:
    """The timeline page: a filtered, paged list of entries, newest first, and the details of the selected one.

    Args:
        ledger: The ledger to read.
        ctx: The request context.
        op: Show only entries with this operation, or all when None.
        trust: Show only entries with this trust level, or all when None.
        before: Show only entries with a smaller ledger sequence number than this (the paging position). When
            None and an event is selected, the list starts a few entries newer than it.
        event_id: The id of the entry to open. An id that does not exist shows a notice instead of details.
    """
    selected = ledger.get_entry(event_id) if event_id else None
    page_before = before
    if before is None and selected is not None:
        before = selected.seq + 6  # start the list near the selected event
    entries = ledger.events_page(before_seq=before, limit=PAGE_SIZE, op=op, trust=trust)
    snaps = {i.id: i for i in ledger.list_snapshots()}

    filters = el("nav", *[
        el("a", label, href=url("/timeline", op=f_op, trust=f_trust),
           aria_current="page" if (f_op, f_trust) == (op, trust) else None)
        for label, f_op, f_trust in FILTERS
    ], class_="filters", aria_label="Filter entries")
    many_stores = _stores(entries)
    rows = [event_row(e, url("/timeline", op=op, trust=trust, before=page_before, event=e.id),
                      selected is not None and e.seq == selected.seq, snaps, many_stores) for e in entries]
    goes_on = len(entries) == PAGE_SIZE
    listing = chain(rows, goes_on) if rows else el("p", "No entries match.")
    pager: list[Markup] = []
    if page_before is not None:
        pager.append(el("a", "Newest", href=url("/timeline", op=op, trust=trust)))
    if goes_on:
        pager.append(el("a", "Older", href=url("/timeline", op=op, trust=trust, before=entries[-1].seq)))
    side = inspector(selected, snaps) if selected else el(
        "div", el("p", "Select an entry to see what changed, where it came from and its place in the ledger."),
        class_="sheet")
    if event_id and selected is None:
        side = notice("That event does not exist.", "bad")
    content = Markup("".join([
        str(el("h1", "Timeline")),
        str(el("p", "Every recorded change, newest entry first. A change found later is stamped with the time it was "
                    "found, so dates can look out of order.", class_="sub")),
        str(filters),
        str(el("div", Markup(str(el("div", listing, el("div", *pager, class_="pager")))), side,
               class_="layout has-sheet" if selected else "layout")),
    ]))
    return Page(200, document(ctx, "Timeline", "timeline", content))


# -- snapshots -----------------------------------------------------------------------------------------------------------

def snapshot_table(infos: list[SnapshotInfo]) -> Markup:
    """A table of snapshots, newest first.

    Each row links to the snapshot and, when the same store has an earlier snapshot in the list, to a comparison
    with it.
    """
    header = el("tr", *[el("th", h, scope="col") for h in ("Snapshot", "Taken", "Store", "Memories", "Anchor", "Label", "")])
    rows = []
    previous: dict[tuple, str] = {}
    for info in sorted(infos, key=lambda i: int(i.id[1:])):
        key = (info.backend, tuple(sorted(info.scope.items())))
        compare = el("a", f"Compare with {previous[key]}", href=url("/diff", **{"from": previous[key], "to": info.id})) \
            if key in previous else Markup("")
        previous[key] = info.id
        rows.append(el(
            "tr",
            el("td", el("a", info.id, href=f"/snapshot/{info.id}")), el("td", el("span", stamp(info.taken_at), class_="mono")),
            el("td", f"{info.backend} ({scope_text(info.scope)})"),
            el("td", info.count, el("br") if not info.complete else None,
               el("span", "may be incomplete", class_="flag") if not info.complete else None),
            el("td", f"after entry {info.ledger_seq}"),
            el("td", el("span", inline(info.label or "", 100), class_="label")), el("td", compare)))
    return el("div", el("table", el("thead", header), el("tbody", *reversed(rows))), class_="table-wrap")


def snapshots(ledger: Ledger, ctx: Context) -> Page:
    """The page listing every snapshot in the ledger."""
    infos = ledger.list_snapshots()
    content = Markup("".join([
        str(el("h1", "Snapshots")),
        str(el("p", "Saved copies of what a memory store held at one moment. Their fingerprints are chained into the ledger.", class_="sub")),
        str(snapshot_table(infos) if infos else el("p", "No snapshots yet. Run 'memdebug snapshot ...'.")),
    ]))
    return Page(200, document(ctx, "Snapshots", "snapshots", content))


def snapshot_detail(ledger: Ledger, ctx: Context, snapshot_id: str, page: int) -> Page:
    """The page for one snapshot: its facts, how to put the store back, and the memories it holds.

    Args:
        ledger: The ledger to read.
        ctx: The request context.
        snapshot_id: The snapshot to show.
        page: Which page of memories to show, counting from 1 (SNAPSHOT_PAGE_SIZE memories per page).

    Raises:
        SnapshotError: If the snapshot cannot be loaded.
    """
    snap = ledger.load_snapshot(snapshot_id)
    info = snap.info
    start = (page - 1) * SNAPSHOT_PAGE_SIZE
    shown = snap.memories[start:start + SNAPSHOT_PAGE_SIZE]
    rows = [el("tr", el("td", inline(m.id, 80)), el("td", el("span", inline(m.text, 120), class_="mem"),
             el("details", el("summary", "full text"), el("pre", block(m.text))))) for m in shown]
    pager: list[Markup] = []
    if page > 1:
        pager.append(el("a", "Previous", href=url(f"/snapshot/{snapshot_id}", page=page - 1)))
    if start + SNAPSHOT_PAGE_SIZE < len(snap.memories):
        pager.append(el("a", "Next", href=url(f"/snapshot/{snapshot_id}", page=page + 1)))
    facts = [("Taken", when(info.taken_at)), ("Store", f"{info.backend} ({scope_text(info.scope)})"),
             ("Memories", str(info.count)), ("Label", info.label or "-"),
             ("Anchored", f"after ledger entry {info.ledger_seq} ({short(info.ledger_head)})")]
    content = Markup("".join([
        str(el("h1", f"Snapshot {info.id}")),
        str(el("p", inline(info.label or "", 100), class_="sub") if info.label else Markup("")),
        str(notice("This snapshot may be incomplete: the listing could have been cut short.") if not info.complete else Markup("")),
        str(el("dl", *[Markup(str(el("dt", k)) + str(el("dd", inline(v, 200)))) for k, v in facts], class_="facts")),
        str(putback(info.backend, info.scope, info.id)),
        str(el("h2", "Memories")),
        str(el("div", el("table", el("thead", el("tr", el("th", "Id", scope="col"), el("th", "Text", scope="col"))),
                         el("tbody", *rows) if rows else el("tbody", el("tr", el("td", "Empty.", colspan="2")))),
               class_="table-wrap")),
        str(el("div", *pager, class_="pager")),
    ]))
    return Page(200, document(ctx, f"Snapshot {info.id}", "snapshots", content))


# -- compare -----------------------------------------------------------------------------------------------------------------

def diff_page(ledger: Ledger, ctx: Context, old_id: str | None, new_id: str | None, full: bool) -> Page:
    """The compare page: a picker for two snapshots and, once both are chosen, what changed between them.

    The picker is a form that sends GET to the same page, so choosing only navigates. A snapshot that cannot be
    loaded is reported on the page rather than raised.

    Args:
        ledger: The ledger to read.
        ctx: The request context.
        old_id: The earlier snapshot, or None if none is chosen.
        new_id: The later snapshot, or None if none is chosen.
        full: Show full texts instead of short excerpts.
    """
    infos = ledger.list_snapshots()
    def options(chosen):
        return [el("option", f"{i.id}: {stamp(i.taken_at)} ({i.count} memories)", value=i.id, selected=(i.id == chosen))
                for i in infos]

    form = el(
        "form",
        el("label", "From (earlier)", el("select", *options(old_id), name="from")),
        el("label", "To (later)", el("select", *options(new_id), name="to")),
        el("label", el("span", el("input", type="checkbox", name="full", value="1", checked=full), "show full texts")),
        el("button", "Compare", type="submit"), class_="pick", method="get", action="/diff",
    )
    parts: list[Markup] = [el("h1", "Compare"), el("p", "What changed between two snapshots.", class_="sub")]
    if len(infos) < 2:
        parts.append(notice("You need at least two snapshots to compare. Run 'memdebug snapshot ...' twice."))
    else:
        parts.append(form)
    if old_id and new_id:
        try:
            result = diff_snapshots(ledger.load_snapshot(old_id), ledger.load_snapshot(new_id))
        except SnapshotError as exc:
            parts.append(notice(str(exc), "bad"))
        else:
            parts += diff_body(result, full)
            parts.append(putback(result.old.backend, result.old.scope, result.old.id, heading=f"Put it back to {result.old.id}",
                                 lead=f"To undo these changes and put the store back to {result.old.id}, run this in a terminal. The page "
                                      "cannot change anything; the first line only shows what would change."))
    return Page(200, document(ctx, "Compare", "compare", Markup("".join(str(p) for p in parts))))


def diff_body(result: Diff, full: bool) -> list[Markup]:
    """The content that describes one comparison: counts, warnings and each change.

    At most MAX_DIFF_CHANGES changes are shown, followed by a notice when there are more. Changed texts are
    Added and removed texts are shown whole with `full`, otherwise as short excerpts. A changed text is marked up
    where that is possible; when it is not, `full` gives a line diff and otherwise a short before-and-after
    excerpt.
    """
    parts: list[Markup] = [el("h2", f"{result.old.id} to {result.new.id}"), el(
        "p", f"{result.count('changed')} changed, {result.count('added')} added, {result.count('removed')} removed "
             f"({result.old.backend}, {scope_text(result.old.scope)})")]
    parts += [notice(w) for w in result.warnings]
    kinds = {"changed": ("UPDATE", "Changed"), "added": ("ADD", "Added"), "removed": ("DELETE", "Removed")}
    items: list[Markup] = []
    for change in result.changes[:MAX_DIFF_CHANGES]:
        label, word = kinds[change.kind]
        who = el("div", el("span", word.upper(), class_=f"badge {OP_CLASS[label]}"), el("strong", inline(short_id(change.memory_id), 80), class_="what"),
                 class_="who")
        if change.kind == "changed":
            marked = redline(change.before or "", change.after or "")
            if marked is not None:
                detail = marked
            elif full:
                detail = diff_lines(line_diff(change.before, change.after))
            else:
                detail = el("p", el("span", inline(change.before, 80), class_="mem"), " became ",
                            el("span", inline(change.after, 80), class_="mem"))
        else:
            text = change.after if change.kind == "added" else change.before
            detail = el("div", block(text, 4000), class_="quote" if change.kind == "added" else "quote gone") if full \
                else el("p", inline(text, 120), class_="mem")
        items.append(el("div", who, el("div", detail), class_="change"))
    parts.append(el("div", *items, class_="changes"))
    if len(result.changes) > MAX_DIFF_CHANGES:
        parts.append(notice(f"Showing the first {MAX_DIFF_CHANGES} of {len(result.changes)} changes."))
    return parts


# -- integrity ---------------------------------------------------------------------------------------------------------------------

def integrity(ctx: Context, counts: dict, result: VerifyResult, checked_at: datetime) -> Page:
    """The integrity page: the outcome of a ledger check and what such a check does and does not prove.

    Args:
        ctx: The request context.
        counts: The ledger's counts, as returned by `Ledger.counts()`; "events", "snapshots" and "head" are shown.
        result: The outcome of the check. At most 100 problems are listed.
        checked_at: When the check ran.
    """
    if result.ok:
        verdict = notice(f"Intact. {counts['events']} events and {counts['snapshots']} snapshots match the chain.", "ok")
    else:
        verdict = el("div", notice("Problems were found. Do not trust this ledger until they are understood.", "bad"),
                     el("ul", *[el("li", p) for p in result.problems[:100]], class_="problems"))
    content = Markup("".join([
        str(el("h1", "Integrity")),
        str(el("p", f"Checked {when(checked_at)}.", class_="sub")),
        str(verdict),
        str(el("h2", "What this does and does not prove")),
        str(el("p", "It proves that no recorded event or snapshot was edited or removed from the middle, and that "
                    "each snapshot still matches its record in the chain.")),
        str(el("p", "It cannot see: someone who rewrites the whole file and rebuilds the chain, or the removal of the "
                    "newest entries unless you compare against a head hash you saved earlier.")),
        str(el("p", "Current head: ", el("code", counts["head"]))),
    ]))
    return Page(200, document(ctx, "Integrity", "integrity", content))
