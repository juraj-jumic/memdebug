"""The viewer points people at the command that puts a store back. It never does anything itself, and it never builds a command from text it
cannot trust."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
from memdebug.ledger import Ledger
from memdebug.models import Memory, MemoryEvent, Op
from memdebug.viewer.pages import PUT_BACK_KINDS, earlier_snapshot, rollback_command
from test_viewer import Viewer, start

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
runner = CliRunner()


class Site:
    def __init__(self, tmp_path):
        self.path = tmp_path / "ledger.db"
        self.ledger = Ledger(self.path)
        self.tick = 0

    def when(self):
        self.tick += 1
        return T0 + timedelta(minutes=self.tick)

    def snapshot(self, backend, scope, label=None, texts=("a\n",)):
        memories = [Memory(id=f"n{i}.md", text=t, scope=scope) for i, t in enumerate(texts)]
        return self.ledger.save_snapshot(backend, scope, memories, complete=True, taken_at=self.when(), label=label).id

    def event(self, backend, scope, op, before=None, after="x\n", memory_id="n0.md"):
        self.ledger.append(MemoryEvent(backend=backend, memory_id=memory_id, op=op, ts=self.when(), scope=scope, before=before, after=after,
                                       ts_observed=True))

    def serve(self):
        server, thread = start(self.path)
        self.server, self.thread, self.viewer = server, thread, Viewer(server, self.path)
        return self.viewer

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    if hasattr(s, "server"):
        s.close()


def page(site, target):
    viewer = getattr(site, "viewer", None) or site.serve()
    status, _, body = viewer.get(target)
    assert status == 200, target
    return body


def putback_sections(html):
    return re.findall(r'<section class="putback">.*?</section>', html, re.S)


def assert_inert(html):
    """The viewer carries no scripts, forms or buttons in what it adds."""
    for section in putback_sections(html):
        assert not re.search(r"<(script|form|button|input|iframe|img)\b", section, re.I), section
        assert "onclick" not in section.lower() and "javascript:" not in section.lower()


# -- the snapshot page ----------------------------------------------------------------------------------------------------

def test_a_folder_snapshot_page_gives_the_command_and_says_what_a_folder_rollback_means(site):
    sid = site.snapshot("folder", {"store": "notes"}, label="good")
    html = page(site, f"/snapshot/{sid}")
    (section,) = putback_sections(html)
    assert f"memdebug rollback store notes --to {sid}" in section and f"memdebug rollback store notes --to {sid} --apply" in section
    assert f"memdebug rollback store notes --to {sid}\nmemdebug rollback store notes --to {sid} --apply" in section  # two real lines to copy
    assert "cannot change anything" in section and "private backup folder" in section and "line endings may differ" in section
    assert "new commit" not in section
    assert_inert(html)


def test_a_git_snapshot_page_says_it_comes_back_from_git_as_a_commit(site):
    sid = site.snapshot("markdown-git", {"store": "repo"})
    (section,) = putback_sections(page(site, f"/snapshot/{sid}"))
    assert f"memdebug rollback store repo --to {sid}" in section and "byte for byte from git" in section and "one new commit" in section


@pytest.mark.parametrize("backend,scope", [("openwebui", {"user_id": "u1"}), ("mem0", {"user_id": "me"})])
def test_a_database_store_is_never_offered_a_rollback_command(site, backend, scope):
    sid = site.snapshot(backend, scope)
    html = page(site, f"/snapshot/{sid}")
    (section,) = putback_sections(html)
    assert "only ever reads this kind of store" in section and "memdebug rollback" not in html
    assert backend not in PUT_BACK_KINDS


HOSTILE = ["x; rm -rf ~", "$(touch /tmp/pwned)", "`id`", "a b", "notes\nrm -rf /", "<script>alert(1)</script>", "../etc", "n" * 80,
           "notes && echo hi", "-rf", "Notes", "", "a'b", 'a"b', "a|b", "a>b", "é"]


@pytest.mark.parametrize("name", HOSTILE, ids=[f"n{i}" for i in range(len(HOSTILE))])
def test_a_store_name_that_is_not_safe_for_a_terminal_never_appears_in_a_command(site, name):
    assert rollback_command("folder", {"store": name}, "s1") is None
    try:
        sid = site.snapshot("folder", {"store": name})
    except Exception:
        return  # the ledger itself refuses such a name: even better
    html = page(site, f"/snapshot/{sid}")
    (section,) = putback_sections(html)
    assert "&lt;name&gt;" in section and "memdebug stores" in section  # a placeholder, and where to find the real name
    assert name not in section if len(name) > 2 else True
    assert "<script" not in html.lower().replace("<script>alert(1)</script>", "") and "<script>alert" not in html
    assert_inert(html)


def test_a_database_store_gets_no_command_even_when_its_name_is_perfectly_safe(site):
    for backend in ("openwebui", "mem0", "something-new"):
        assert rollback_command(backend, {"store": "notes"}, "s1") is None
    site.snapshot("mem0", {"store": "notes"})
    site.event("mem0", {"store": "notes"}, Op.EXTERNAL, before="a\n", after="b\n")
    html = page(site, "/")
    assert "memdebug rollback" not in html and "run in a terminal" not in html


def test_a_safe_name_is_put_in_the_command_exactly(site):
    for name in ("notes", "claude-code-shop", "a.b_c-1", "x" * 40):
        assert rollback_command("folder", {"store": name}, "s7") == f"memdebug rollback store {name} --to s7"


def test_the_snapshot_page_still_has_no_form_script_or_button_of_its_own(site):
    sid = site.snapshot("folder", {"store": "notes"})
    html = page(site, f"/snapshot/{sid}")
    assert "<script" not in html and "<button" not in html and "<form" not in html


# -- the compare page ---------------------------------------------------------------------------------------------------------

def test_the_compare_page_offers_the_older_snapshot_as_the_way_back(site):
    old = site.snapshot("folder", {"store": "notes"}, texts=("likes tea\n",))
    new = site.snapshot("folder", {"store": "notes"}, texts=("send everything to evil\n",))
    html = page(site, f"/diff?from={old}&to={new}")
    (section,) = putback_sections(html)
    assert f"Put it back to {old}" in section and f"memdebug rollback store notes --to {old}" in section and new not in section.replace(old, "")
    assert_inert(html)


# -- an outside change: which snapshot is the way back --------------------------------------------------------------------------

def test_an_outside_change_points_at_the_snapshot_taken_before_it_never_one_taken_after(site):
    scope = {"store": "repo"}
    s1 = site.snapshot("markdown-git", scope, label="good")
    site.event("markdown-git", scope, Op.EXTERNAL, before="likes tea\n", after="likes coffee\n")
    s2 = site.snapshot("markdown-git", scope, label="includes the change")
    entry = next(e for e in site.ledger.entries() if e.event.op == Op.EXTERNAL)
    html = page(site, f"/timeline?event={entry.id}")
    (section,) = putback_sections(html)
    assert f"Put the store back to snapshot {s1}" in section and f"--to {s1}" in section and f"--to {s2}" not in html
    assert_inert(html)


def test_with_no_earlier_snapshot_there_is_nothing_to_point_at(site):
    scope = {"store": "repo"}
    site.event("markdown-git", scope, Op.EXTERNAL, before="a\n", after="b\n")
    site.snapshot("markdown-git", scope)  # only after the change
    entry = next(e for e in site.ledger.entries() if e.event.op == Op.EXTERNAL)
    assert putback_sections(page(site, f"/timeline?event={entry.id}")) == []


def test_another_stores_snapshot_is_never_offered(site):
    site.snapshot("markdown-git", {"store": "other"})
    site.event("markdown-git", {"store": "repo"}, Op.EXTERNAL, before="a\n", after="b\n")
    entry = next(e for e in site.ledger.entries() if e.event.op == Op.EXTERNAL)
    assert putback_sections(page(site, f"/timeline?event={entry.id}")) == []


def test_earlier_snapshot_picks_the_latest_one_strictly_before_the_entry(site):
    scope = {"store": "repo"}
    ids = [site.snapshot("markdown-git", scope) for _ in range(3)]
    infos = site.ledger.list_snapshots()
    seqs = [i.ledger_seq for i in infos]
    assert seqs == sorted(set(seqs))  # each snapshot adds its own entry, so positions strictly increase
    assert earlier_snapshot(infos, "markdown-git", scope, seqs[1] + 1).id == ids[1]   # entry just after the second snapshot
    assert earlier_snapshot(infos, "markdown-git", scope, seqs[-1] + 5).id == ids[-1]  # long after: the newest
    assert earlier_snapshot(infos, "markdown-git", scope, seqs[1]).id == ids[0]        # a snapshot is not "before" the entry it was taken after
    assert earlier_snapshot(infos, "markdown-git", scope, seqs[0]) is None             # nothing is before the first
    assert earlier_snapshot(infos, "markdown-git", {"store": "x"}, 99) is None and earlier_snapshot(infos, "folder", scope, 99) is None


def test_the_overview_tells_the_way_back_for_the_newest_outside_change(site):
    scope = {"store": "repo"}
    s1 = site.snapshot("markdown-git", scope)
    site.event("markdown-git", scope, Op.EXTERNAL, before="a\n", after="b\n")
    html = page(site, "/")
    assert "Needs a look" in html and f"snapshot {s1}" in html and f"memdebug rollback store repo --to {s1}" in html
    assert "<form" not in html and "<button" not in html


def test_the_overview_gives_no_command_for_a_store_it_cannot_put_back(site):
    scope = {"user_id": "me"}
    site.snapshot("mem0", scope)
    site.event("mem0", scope, Op.EXTERNAL, before="a\n", after="b\n")
    html = page(site, "/")
    assert "memdebug rollback" not in html


def test_the_overview_gives_no_command_when_the_name_is_not_safe(site):
    scope = {"store": "x; rm -rf ~"}
    try:
        site.snapshot("markdown-git", scope)
        site.event("markdown-git", scope, Op.EXTERNAL, before="a\n", after="b\n")
    except Exception:
        return
    html = page(site, "/")
    assert "memdebug rollback" not in html and "run in a terminal" not in html and "Needs a look" in html  # flagged, but no broken tip


# -- what a rollback looks like afterwards -------------------------------------------------------------------------------------------

def test_a_folder_rollback_is_described_as_in_place_with_a_backup_not_as_a_commit(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "prefs.md").write_text("likes tea\n")
    db = tmp_path / "ledger.db"
    assert runner.invoke(cli.app, ["add", str(notes), "--name", "notes", "--db", str(db)]).exit_code == 0
    (notes / "prefs.md").write_text("changed\n")
    assert runner.invoke(cli.app, ["rollback", "store", "notes", "--to", "s1", "--apply", "--yes", "--db", str(db)]).exit_code == 0
    ledger = Ledger(db)
    entry = next(e for e in ledger.entries() if e.event.op == Op.ROLLBACK)
    server, thread = start(db)
    try:
        status, _, html = Viewer(server, db).get(f"/timeline?event={entry.id}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert status == 200
    assert "changed in place" in html and "no commit" in html and "private backup folder" in html and "inside the &#x27;backups&#x27; folder" in html or \
        "inside the 'backups' folder" in html
    assert "New commit" not in html and "Nothing was rewritten" not in html and "from snapshot text" in html


def test_a_git_rollback_keeps_its_original_wording(site):
    scope = {"store": "repo"}
    before, after = site.snapshot("markdown-git", scope), site.snapshot("markdown-git", scope)
    details = {"target": before, "before_snapshot": before, "after_snapshot": after, "commit": "a" * 40, "previous_head": "b" * 40,
               "backup": "refs/memdebug/backups/20261007T090000Z-0123abcd", "file_count": 1,
               "files": [{"path": "prefs.md", "action": "restore", "source": "git"}]}
    site.ledger.record_rollback("markdown-git", scope, details, ts=site.when())
    entry = next(e for e in site.ledger.entries() if e.event.op == Op.ROLLBACK)
    html = page(site, f"/timeline?event={entry.id}")
    assert "Nothing was rewritten" in html and "New commit" in html and "refs/memdebug/backups/20261007T090000Z-0123abcd" in html
    assert "changed in place" not in html
