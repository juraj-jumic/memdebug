"""Reports: memory text is untrusted and must stay inert in every format."""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
from memdebug import report as r
from memdebug import witness as w
from memdebug.errors import MemdebugError
from memdebug.ledger import Ledger
from memdebug.models import MemoryEvent, Op
from payloads import PAYLOAD_IDS, PAYLOADS

runner = CliRunner()
T = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")
HEADINGS = {"# memdebug report", "## Summary", "### Integrity problems", "## Findings", "## Rollbacks", "## Snapshots"}


def ledger_with(path, memory_id="prefs.md", before="old\n", after="new\n", op=Op.EXTERNAL, trust_source=None):
    ledger = Ledger(path)
    ledger.append(MemoryEvent(backend="markdown-git", memory_id=memory_id, op=op, ts=T, scope={"store": "n"}, before=before, after=after,
                              source=trust_source, ts_observed=True))
    return ledger


def outside_code(markdown):
    """The Markdown with fenced blocks and inline code removed: what a renderer would treat as live formatting."""
    lines, out, fence = markdown.split("\n"), [], None
    for line in lines:
        m = re.match(r"^(`{3,})", line)
        if fence is None and m:
            fence = m.group(1)
            continue
        if fence is not None:
            if line.startswith(fence) and set(line.strip()) == {"`"}:
                fence = None
            continue
        out.append(re.sub(r"(`+)(?:(?!\1).)+\1", "", line))
    return "\n".join(out)


def test_a_report_says_what_needs_a_look(tmp_path):
    rep = r.build_report(ledger_with(tmp_path / "l.db"), ledger_name="l.db", now=T)
    text = r.to_markdown(rep)
    assert rep.attention and "**INTACT**" in text and "outside-history (warning)" in text and "Before:" in text and "new" in text
    assert [f.rule for f in rep.findings] == ["outside-history"] and rep.counts["outside_history"] == 1


def test_a_quiet_ledger_reports_nothing_to_look_at(tmp_path):
    ledger = ledger_with(tmp_path / "l.db", op=Op.ADD, before=None, after="x")
    rep = r.build_report(ledger, now=T)
    assert not rep.attention and "Nothing needs a look." in r.to_markdown(rep)


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_hostile_text_never_becomes_live_markdown_html_or_terminal_escapes(tmp_path, payload):
    ledger = ledger_with(tmp_path / "h.db", memory_id=payload[:100] or "x", before=payload, after=payload + "\n# not a heading\n[l](javascript:alert(1))")
    rep = r.build_report(ledger, ledger_name=payload[:50], now=T)
    markdown = r.to_markdown(rep)
    live = outside_code(markdown)
    assert "<" not in live and "[l](" not in live and "javascript:" not in live
    assert all(line in HEADINGS or not line.startswith("#") or re.match(r"^### \d+\. (outside-history|untrusted-source|integrity|witness|hint)", line) for line in live.split("\n"))
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069\u200b-\u200f]", markdown)
    for blob in (r.to_json(rep), r.to_sarif(rep)):
        assert json.loads(blob) and not re.search(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]", blob)


def test_a_fence_is_always_longer_than_any_backtick_run_in_the_text(tmp_path):
    nasty = "before\n" + "`" * 3 + "\n" + "`" * 9 + "\n```` x\n</pre>\nafter"
    markdown = r.to_markdown(r.build_report(ledger_with(tmp_path / "f.db", after=nasty), now=T))
    assert "`" * 10 in markdown and "<" not in outside_code(markdown)


def test_long_text_is_cut_and_says_so(tmp_path):
    rep = r.build_report(ledger_with(tmp_path / "b.db", after="x" * 50_000), now=T)
    assert len(rep.findings[0].after) < 2000 and "more characters" in rep.findings[0].after


def test_json_has_the_documented_shape(tmp_path):
    data = json.loads(r.to_json(r.build_report(ledger_with(tmp_path / "j.db"), now=T)))
    assert set(data) == {"tool", "generated_at", "ledger", "integrity", "witness", "counts", "attention", "truncated", "findings", "rollbacks", "snapshots"}
    assert data["attention"] is True and data["findings"][0]["rule"] == "outside-history" and data["witness"] is None


def test_sarif_is_valid_in_the_ways_that_matter(tmp_path):
    ledger = Ledger(tmp_path / "s.db")
    for name in ["notes/a b.md", "../escape.md", "a/../../etc/x.md", "uuid-0000-1111", "x:y.md"]:
        ledger.append(MemoryEvent(backend="m", memory_id=name, op=Op.EXTERNAL, ts=T, scope={"s": "1"}, before="a", after="b"))
    doc = json.loads(r.to_sarif(r.build_report(ledger, now=T)))
    run = doc["runs"][0]
    assert doc["version"] == "2.1.0" and run["tool"]["driver"]["name"] == "memdebug"
    assert {rule["id"] for rule in run["tool"]["driver"]["rules"]} >= {"outside-history", "integrity"}
    uris = [loc["physicalLocation"]["artifactLocation"]["uri"] for res in run["results"] for loc in res["locations"] if "physicalLocation" in loc]
    assert uris == ["notes/a%20b.md"]  # path-like ids only, percent-encoded; escapes, colons and non-paths get logical locations only
    assert all(res["ruleId"] in {rule["id"] for rule in run["tool"]["driver"]["rules"]} for res in run["results"])


def test_a_broken_ledger_is_loudly_reported(tmp_path):
    path = tmp_path / "t.db"
    ledger_with(path).close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE events SET payload = replace(payload, 'new', 'evil')")
    raw.commit()
    raw.close()
    rep = r.build_report(Ledger(path), now=T)
    assert not rep.integrity_ok and rep.findings[0].rule == "integrity" and "PROBLEMS FOUND" in r.to_markdown(rep)


def test_a_witness_disagreement_is_a_finding(tmp_path):
    base = ledger_with(tmp_path / "a.db")
    wp = tmp_path / "w.txt"
    w.append_witness(base, wp, now=T)
    other = ledger_with(tmp_path / "b.db", after="different")
    rep = r.build_report(other, witness=w.verify_witness(other, wp), now=T)
    assert rep.findings[0].rule == "witness" and "DISAGREES" in r.to_markdown(rep)


# -- writing a report file ------------------------------------------------------------------------------------------

def test_a_report_file_is_private_never_overwritten_by_accident_and_leaves_nothing_behind(tmp_path):
    target = tmp_path / "out.md"
    r.write_report(target, "first")
    assert target.read_text() == "first" and not [p for p in tmp_path.iterdir() if p.name.startswith(".memdebug")]
    if os.name == "posix":
        assert target.stat().st_mode & 0o077 == 0
    with pytest.raises(MemdebugError, match="already exists"):
        r.write_report(target, "second")
    r.write_report(target, "second", force=True)
    assert target.read_text() == "second"
    with pytest.raises(MemdebugError, match="does not exist"):
        r.write_report(tmp_path / "nope" / "x.md", "x")
    with pytest.raises(MemdebugError, match="plain file"):
        r.write_report(tmp_path, "x", force=True)


@posix_only
def test_a_report_is_never_written_through_a_link(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    link = tmp_path / "out.md"
    os.symlink(victim, link)
    with pytest.raises(MemdebugError, match="plain file"):
        r.write_report(link, "x", force=True)
    assert victim.read_text() == "keep"


# -- the command ----------------------------------------------------------------------------------------------------

def test_the_command_formats_and_exit_codes(tmp_path):
    db = tmp_path / "l.db"
    ledger_with(db).close()
    assert runner.invoke(cli.app, ["report", "--db", str(db)]).exit_code == 0
    assert runner.invoke(cli.app, ["report", "--db", str(db), "--fail-on", "findings"]).exit_code == 1
    out = tmp_path / "r.sarif"
    ok = runner.invoke(cli.app, ["report", "--db", str(db), "-f", "sarif", "-o", str(out)])
    assert ok.exit_code == 0 and json.loads(out.read_text())["version"] == "2.1.0"
    assert runner.invoke(cli.app, ["report", "--db", str(db), "-f", "html"]).exit_code == 2
    assert runner.invoke(cli.app, ["report", "--db", str(db), "-o", str(out)]).exit_code == 2  # exists, no --force


def test_the_command_fails_on_a_broken_ledger_even_without_fail_on(tmp_path):
    db = tmp_path / "t.db"
    ledger_with(db).close()
    raw = sqlite3.connect(db)
    raw.execute("UPDATE events SET payload = replace(payload, 'new', 'evil')")
    raw.commit()
    raw.close()
    assert runner.invoke(cli.app, ["report", "--db", str(db)]).exit_code == 1
