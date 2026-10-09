"""Reports on a ledger: for a person (Markdown), for a program (JSON) and for CI and code-scanning dashboards (SARIF).

Everything that came from a memory store is untrusted text. In Markdown it only ever appears inside a code fence that
is longer than any run of backticks in the text, so it cannot turn into headings, links or HTML. Control and
bidirectional characters are made visible. In JSON and SARIF it is a plain string value, bounded in length.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from . import __version__
from .describe import rollback_details
from .errors import MemdebugError
from .hints import new_hints
from .ledger import Ledger
from .models import Op, Trust
from .textsafe import safe_text
from .witness import WitnessCheck

MAX_FINDINGS = 500
MAX_TEXT = 1500

RULES = {
    "outside-history": ("A memory changed outside its store's own history", "warning",
                        "A memory was added, changed or removed without going through the store's own history (an uncommitted file "
                        "edit, a direct database change). Check whether you made the change."),
    "untrusted-source": ("A memory came from an untrusted source", "warning",
                         "The recorded source of this memory is an untrusted one (for example a tool result the agent read)."),
    "integrity": ("The ledger failed its integrity check", "error",
                  "The ledger's hash chain or a snapshot does not match what was recorded. Do not trust it until this is understood."),
    "hint": ("Worth a second look", "note",
             "The wording of what was added looks like a known poisoning pattern (an instruction to send data to an address, to stop "
             "asking for confirmation, to weaken a safeguard, hidden characters, or a secret). A heuristic: it misses things and can be wrong."),
    "witness": ("The ledger disagrees with its witness", "error",
                "The ledger was rewritten or cut short compared with the witness file."),
}


@dataclass
class Finding:
    """One thing in a report that may need a person's attention.

    Attributes:
        rule: The identifier of the rule that raised it, a key of `RULES`.
        level: "error", "warning" or "note".
        message: A short description of the finding.
        memory_id: The memory it concerns, if any.
        event_id: The ledger entry that recorded it, if any.
        when: When that entry was recorded, as UTC timestamp text.
        before: The memory's text before the change, if any.
        after: The memory's text after the change, if any.
        evidence: The wording that raised a "hint" finding.
    """

    rule: str
    level: str
    message: str
    memory_id: str | None = None
    event_id: str | None = None
    when: str | None = None
    before: str | None = None
    after: str | None = None
    evidence: str | None = None


@dataclass
class Report:
    """The content of a report on a ledger, before it is rendered to a format.

    Attributes:
        generated_at: When the report was built, as UTC timestamp text.
        tool_version: The memdebug version that built it.
        ledger_name: A display name for the ledger.
        integrity_ok: Whether the ledger passed its integrity check.
        integrity_problems: What the integrity check found, if anything.
        witness: The result of checking the ledger against a witness file, if one was given.
        counts: Totals for entries, snapshots, changes outside the store's history, untrusted memories, rollbacks and hints.
        findings: The things that may need a look, at most `MAX_FINDINGS` of the per-entry kind.
        rollbacks: One dict per rollback recorded in the ledger.
        snapshots: One dict per snapshot in the ledger.
        truncated: Whether the findings were cut off at `MAX_FINDINGS`.
    """

    generated_at: str
    tool_version: str
    ledger_name: str
    integrity_ok: bool
    integrity_problems: list[str]
    witness: WitnessCheck | None
    counts: dict
    findings: list[Finding] = field(default_factory=list)
    rollbacks: list[dict] = field(default_factory=list)
    snapshots: list[dict] = field(default_factory=list)
    truncated: bool = False

    @property
    def hinted(self) -> bool:
        """Whether any finding is a wording hint."""
        return any(f.rule == "hint" for f in self.findings)

    @property
    def attention(self) -> bool:
        """Whether something needs a look.

        True if the integrity check failed, the witness disagrees, or any finding is a warning or an error. Wording hints
        alone do not count.
        """
        return (not self.integrity_ok or (self.witness is not None and not self.witness.ok)
                or any(f.level in ("warning", "error") for f in self.findings))


def _text(value: str | None) -> str | None:
    """Memory text, bounded, with control and bidi characters made visible; line breaks are kept."""
    if value is None:
        return None
    lines = value.replace("\r\n", "\n").split("\n")
    out = "\n".join(safe_text(line, None) for line in lines)
    return out if len(out) <= MAX_TEXT else out[:MAX_TEXT] + f"\n... [{len(out) - MAX_TEXT} more characters]"


def build_report(ledger: Ledger, *, ledger_name: str = "ledger", witness: WitnessCheck | None = None,
                 now: datetime | None = None) -> Report:
    """Read a ledger and build a report on it.

    The ledger's integrity is verified and every entry is looked at. Memory text is made visible with `safe_text` and cut to
    `MAX_TEXT` characters. Nothing is written.

    Args:
        ledger: The ledger to report on.
        ledger_name: A display name for the ledger, shown in the report.
        witness: The result of checking the ledger against a witness file, if one was checked.
        now: The time to record as the generation time. Defaults to the current time.
    """
    verdict = ledger.verify()
    counts = ledger.counts()
    report = Report(
        generated_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tool_version=__version__, ledger_name=safe_text(ledger_name, 80), integrity_ok=verdict.ok,
        integrity_problems=[safe_text(p, 300) for p in verdict.problems[:50]], witness=witness,
        counts={"events": counts["events"], "snapshots": counts["snapshots"], "outside_history": counts["by_op"].get("EXTERNAL", 0),
                "untrusted": counts["untrusted"], "rollbacks": counts["by_op"].get("ROLLBACK", 0), "hints": 0},
    )
    for entry in ledger.entries():
        event = entry.event
        when = event.ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if event.op == Op.EXTERNAL and len(report.findings) < MAX_FINDINGS:
            report.findings.append(Finding("outside-history", "warning",
                                           f"{safe_text(event.memory_id, 80)} changed outside the store's own history",
                                           safe_text(event.memory_id, 120), entry.id, when, _text(event.before), _text(event.after)))
        elif event.trust == Trust.UNTRUSTED and event.op not in (Op.SNAPSHOT, Op.SNAPSHOT_DELETED, Op.ROLLBACK) and len(report.findings) < MAX_FINDINGS:
            report.findings.append(Finding("untrusted-source", "warning",
                                           f"{safe_text(event.memory_id, 80)} came from an untrusted source",
                                           safe_text(event.memory_id, 120), entry.id, when, _text(event.before), _text(event.after)))
        if event.op in (Op.ADD, Op.UPDATE, Op.EXTERNAL):
            for hint in [h for h in new_hints(event.before, event.after, budget=0.1, max_chars=20_000) if h.severity == "warning"][:3]:
                if len(report.findings) < MAX_FINDINGS:
                    report.findings.append(Finding("hint", "note", hint.message, safe_text(event.memory_id, 120), entry.id, when, evidence=hint.evidence))
        if event.op == Op.ROLLBACK:
            details = rollback_details(event)
            if details is not None:
                report.rollbacks.append({"id": entry.id, "rollback": event.memory_id, "when": when, "target": details["target"],
                                         "files": details["file_count"], "undo_point": details["before_snapshot"],
                                         "commit": details["commit"], "backup": details["backup"]})
    report.truncated = len(report.findings) >= MAX_FINDINGS
    report.counts["hints"] = sum(1 for f in report.findings if f.rule == "hint")
    for info in ledger.list_snapshots():
        report.snapshots.append({"id": info.id, "taken": info.taken_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "memories": info.count, "label": safe_text(info.label or "", 100), "complete": info.complete})
    if not verdict.ok:
        report.findings.insert(0, Finding("integrity", "error", "the ledger failed its integrity check: " + (report.integrity_problems[0] if report.integrity_problems else "")))
    if witness is not None and not witness.ok:
        report.findings.insert(0, Finding("witness", "error", "the ledger disagrees with its witness: " + safe_text(witness.problems[0] if witness.problems else "", 300)))
    return report


# -- Markdown -------------------------------------------------------------------------------------------------------

def _fence(text: str) -> str:
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code_block(text: str | None) -> str:
    if text is None:
        return "_(none)_\n"
    fence = _fence(text)
    return f"{fence}text\n{text}\n{fence}\n"


def _code_span(text: str) -> str:
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def to_markdown(report: Report) -> str:
    """Render the report as Markdown for a person to read.

    Memory text only appears inside code fences or code spans longer than any run of backticks in it.
    """
    out = ["# memdebug report", "",
           f"Generated {report.generated_at} by memdebug {report.tool_version}. Ledger: {_code_span(report.ledger_name)}.", ""]
    verdict = "INTACT" if report.integrity_ok else "PROBLEMS FOUND"
    out += ["## Summary", "",
            f"- Ledger integrity: **{verdict}**",
            f"- Entries: {report.counts['events']}, snapshots: {report.counts['snapshots']}, rollbacks: {report.counts['rollbacks']}",
            f"- Changes outside the store's own history: **{report.counts['outside_history']}**",
            f"- Memories from an untrusted source: {report.counts['untrusted']}",
            f"- Wording worth a second look (heuristic hints): {report.counts['hints']}"]
    if report.witness is not None:
        w = report.witness
        out.append(f"- Witness: **{'agrees' if w.ok else 'DISAGREES'}** ({w.lines} witnessed head(s), {w.unwitnessed} newer entries not yet witnessed)")
    if not report.integrity_ok:
        out += ["", "### Integrity problems", ""] + [f"- {_code_span(p)}" for p in report.integrity_problems]
    out += ["", "## Findings", ""]
    if not report.findings:
        out.append("Nothing needs a look.")
    for number, f in enumerate(report.findings, 1):
        out += [f"### {number}. {f.rule} ({f.level})", "", f"{_code_span(f.message)}", ""]
        if f.memory_id:
            out.append(f"- Memory: {_code_span(f.memory_id)}")
        if f.event_id:
            out.append(f"- Ledger entry: {_code_span(f.event_id)}, noticed {f.when}")
        if f.evidence:
            out.append(f"- Evidence: {_code_span(f.evidence)}")
        if f.before is not None or f.after is not None:
            out += ["", "Before:", "", _code_block(f.before), "After:", "", _code_block(f.after)]
        out.append("")
    if report.truncated:
        out.append(f"_Only the first {MAX_FINDINGS} findings are shown._\n")
    if report.rollbacks:
        out += ["## Rollbacks", ""]
        for r in report.rollbacks:
            out.append(f"- {_code_span(r['rollback'])} at {r['when']}: back to {_code_span(r['target'])}, {r['files']} file(s); "
                       f"undo point {_code_span(r['undo_point'] or '-')}")
        out.append("")
    if report.snapshots:
        out += ["## Snapshots", ""]
        for s in report.snapshots:
            out.append(f"- {_code_span(s['id'])} {s['taken']}: {s['memories']} memories" + (f", {_code_span(s['label'])}" if s["label"] else ""))
        out.append("")
    out += ["---", "memdebug records what a memory store holds and what changed. It does not say who or what caused a change, and "
            "it is not a prompt-injection detector. See docs/threat-model.md.", ""]
    return "\n".join(out)


# -- JSON -----------------------------------------------------------------------------------------------------------

def to_dict(report: Report) -> dict:
    """The report as a JSON-serialisable dict, with an `attention` flag added."""
    return {
        "tool": {"name": "memdebug", "version": report.tool_version}, "generated_at": report.generated_at, "ledger": report.ledger_name,
        "integrity": {"ok": report.integrity_ok, "problems": report.integrity_problems},
        "witness": None if report.witness is None else {"ok": report.witness.ok, "problems": [safe_text(p, 300) for p in report.witness.problems],
                                                        "lines": report.witness.lines, "unwitnessed": report.witness.unwitnessed},
        "counts": report.counts, "attention": report.attention, "truncated": report.truncated,
        "findings": [{"rule": f.rule, "level": f.level, "message": f.message, "memory_id": f.memory_id, "ledger_entry": f.event_id,
                      "noticed": f.when, "before": f.before, "after": f.after, "evidence": f.evidence} for f in report.findings],
        "rollbacks": report.rollbacks, "snapshots": report.snapshots,
    }


def to_json(report: Report) -> str:
    """Render the report as indented, ASCII-only JSON ending in a newline."""
    return json.dumps(to_dict(report), indent=2, ensure_ascii=True) + "\n"


# -- SARIF ----------------------------------------------------------------------------------------------------------

_PATHLIKE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/ ]{0,200}\.[A-Za-z0-9]{1,8}\Z")  # a file name with an extension, not just any id


def to_sarif(report: Report) -> str:
    """Render the report as SARIF 2.1.0 JSON, for CI and code-scanning dashboards.

    A finding's memory id becomes a file location only if it looks like a plain relative file name; it is always given as a
    logical location.
    """
    rules = [{"id": rid, "name": rid, "shortDescription": {"text": short}, "fullDescription": {"text": full},
              "defaultConfiguration": {"level": level}} for rid, (short, level, full) in RULES.items()]
    results = []
    for f in report.findings:
        item: dict = {"ruleId": f.rule, "level": f.level, "message": {"text": f.message},
                      "properties": {"ledger_entry": f.event_id, "noticed": f.when, "evidence": f.evidence}}
        if f.memory_id:
            location: dict = {"logicalLocations": [{"name": f.memory_id, "kind": "memory"}]}
            if _PATHLIKE.match(f.memory_id) and ".." not in f.memory_id.split("/"):
                location["physicalLocation"] = {"artifactLocation": {"uri": quote(f.memory_id, safe="/")}}
            item["locations"] = [location]
        results.append(item)
    document = {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
                "runs": [{"tool": {"driver": {"name": "memdebug", "version": report.tool_version, "rules": rules}}, "results": results}]}
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


RENDERERS = {"markdown": to_markdown, "json": to_json, "sarif": to_sarif}


def write_report(path, text: str, *, force: bool = False) -> None:
    """Write a report file.

    It contains memory text, so it is created private; an existing file is only replaced with `force`, and links and folders
    are refused. The text is written to a temporary file beside the target and then moved into place.

    Raises:
        MemdebugError: If the target is a link or a folder, already exists without `force`, or its folder does not exist.
    """
    from pathlib import Path

    target = Path(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MemdebugError("the report path must be a plain file, not a link or a folder")
        if not force:
            raise MemdebugError("that file already exists; use --force to replace it")
    if not target.parent.is_dir():
        raise MemdebugError("the folder for the report does not exist")
    temporary = target.parent / f".memdebug-report-{secrets.token_hex(6)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
