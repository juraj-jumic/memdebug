import sqlite3
from datetime import datetime, timezone

import pytest

from memdebug.ledger import Ledger
from memdebug.models import Memory
from test_viewer import Viewer, start

T = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SCOPE = {"store": "notes"}
DETAILS = {"target": "s1", "before_snapshot": "s2", "after_snapshot": "s3", "commit": "a" * 40, "previous_head": "b" * 40,
           "backup": "refs/memdebug/backups/20261005T120000Z-0123abcd", "file_count": 3,
           "files": [{"path": "people.md", "action": "restore", "source": "git"},
                     {"path": "old/note.md", "action": "recreate", "source": "snapshot"},
                     {"path": "added.md", "action": "remove", "source": "none"}]}


def build(tmp_path):
    path = tmp_path / "rb.db"
    ledger = Ledger(path)
    memories = [Memory(id="people.md", text="Ann", scope=SCOPE)]
    for label in ("baseline", "before rollback to s1", "after rollback to s1"):
        ledger.save_snapshot("markdown-git", SCOPE, memories, complete=True, taken_at=T, label=label)
    ledger.record_rollback("markdown-git", SCOPE, DETAILS, ts=T)
    ledger.close()
    return path


@pytest.fixture
def viewer(tmp_path):
    path = build(tmp_path)
    server, thread = start(path)
    yield Viewer(server, path)
    server.shutdown(); server.server_close(); thread.join(5)


def test_a_rollback_has_its_own_row_icon_badge_and_filter(viewer):
    _, _, body = viewer.get("/timeline")
    assert '<li class="n-rollback">' in body and 'class="badge op-rollback">ROLLBACK</span>' in body
    assert "back to s1" in body and "3 files changed" in body
    assert ">Rollbacks</a>" in body
    _, _, only = viewer.get("/timeline?op=ROLLBACK")
    assert only.count('class="row"') == 1 and 'n-rollback' in only and "n-snapshot" not in only


def test_the_detail_view_says_what_was_done_and_links_to_the_undo_point(viewer):
    _, _, body = viewer.get("/timeline?event=e4")
    assert "Rollback r1" in body and "What was done" in body
    assert 'href="/snapshot/s1"' in body and 'href="/snapshot/s2"' in body and 'href="/snapshot/s3"' in body
    assert "restore " in body and "people.md" in body and " from git" in body and " from snapshot text" in body
    assert "refs/memdebug/backups/20261005T120000Z-0123abcd" in body and ("a" * 12) in body and ("b" * 12) in body
    assert "Trust" not in body.split("Details")[0]


def test_a_tampered_rollback_record_is_shown_as_unreadable_and_never_as_markup(tmp_path):
    path = build(tmp_path)
    raw = sqlite3.connect(path)
    payload = raw.execute("SELECT payload FROM events WHERE op = 'ROLLBACK'").fetchone()[0]
    evil = payload.replace("people.md", "<script>alert(1)</script>.md", 1)
    assert evil != payload
    raw.execute("UPDATE events SET payload = ? WHERE op = 'ROLLBACK'", (evil,))
    raw.commit()
    raw.close()
    server, thread = start(path)
    try:
        viewer = Viewer(server, path)
        _, _, detail = viewer.get("/timeline?event=e4")
        _, _, listing = viewer.get("/timeline")
        assert "could not be read" in detail and "<script" not in detail.lower() and "alert(1)" not in detail
        assert "unreadable record" in listing and "<script" not in listing.lower()
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
