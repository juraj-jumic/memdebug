"""Finding which logged agent call wrote a note. Every log here is made up; the logs on a real computer are whole conversations and are never used."""
import json
import os
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

import memdebug.provenance as pv
from memdebug.provenance import Provenance, Writer, find_writers

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows file names ignore case")

START = datetime(2026, 10, 7, 18, 29, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=30)
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
SECRET = "SECRET-LOG-TEXT"


def uid(n):
    return f"{n:08x}-0000-4000-8000-000000000000"


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "home"
    (folder / ".claude" / "projects" / "-p1" / "memory").mkdir(parents=True)
    return folder


def note(home, name="note.md"):
    return str(home / ".claude" / "projects" / "-p1" / "memory" / name)


def call(tool="Write", at="2026-10-07T18:29:03.104Z", rec=1, session=SESSION, **given):
    return {"type": "assistant", "sessionId": session, "uuid": uid(rec), "timestamp": at, "cwd": SECRET,
            "message": {"role": "assistant", "content": [{"type": "text", "text": SECRET},
                                                         {"type": "tool_use", "id": "toolu_1", "name": tool, "input": given}]}}


def make_log(home, records, name="s1.jsonl", project="-p1", mtime=END, raw=()):
    path = home / ".claude" / "projects" / project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in records) + b"".join(raw)
    path.write_bytes(data)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def find(home, path=None, start=START, end=END):
    return find_writers(path or note(home), start, end, home)


def test_a_logged_write_to_the_path_in_the_period_is_found(home):
    make_log(home, [call(file_path=note(home), content=SECRET), call(rec=2, file_path=note(home, "other.md"))])
    result = find(home)
    assert result == Provenance((Writer(SESSION, uid(1), "Write", datetime(2026, 10, 7, 18, 29, 3, 104000, tzinfo=timezone.utc)),), 0, 1, True)


def test_calls_outside_the_period_or_on_other_files_are_not_matched(home):
    make_log(home, [call(at="2026-10-07T18:40:00.000Z", file_path=note(home)), call(at="2026-10-07T18:28:59.000Z", rec=2, file_path=note(home)),
                    call(rec=3, file_path=note(home, "other.md"))])
    assert find(home).writers == ()


def test_the_edit_tools_count_and_reading_does_not(home):
    make_log(home, [call("Edit", rec=1, file_path=note(home)), call("MultiEdit", rec=2, file_path=note(home)),
                    call("NotebookEdit", rec=3, notebook_path=note(home)), call("Read", rec=4, file_path=note(home)),
                    call("Grep", rec=5, path=note(home)), call("Write", rec=6, path=note(home))])  # wrong field for Write
    assert sorted(w.tool for w in find(home).writers) == ["Edit", "MultiEdit", "NotebookEdit"]


def test_paths_are_compared_after_cleaning_them_up(home):
    messy = note(home).replace("memory", "memory" + os.sep + "x" + os.sep + "..")
    make_log(home, [call(file_path=messy)])
    assert len(find(home).writers) == 1


@windows_only
def test_on_windows_case_and_slash_style_do_not_matter(home):
    make_log(home, [call(file_path=note(home).upper().replace("\\", "/"))])
    assert len(find(home).writers) == 1


@posix_only
def test_on_posix_a_different_case_is_a_different_file(home):
    make_log(home, [call(file_path=note(home).upper())])
    assert find(home).writers == ()


BAD_RECORDS = {
    "session-newline": call(session=SESSION + "\n", file_path="{n}"),
    "session-not-uuid": call(session="not-a-uuid", file_path="{n}"),
    "session-upper": call(session="abcdefab-abcd-4abc-8abc-abcdefabcdef".upper(), file_path="{n}"),
    "uuid-newline": {**call(file_path="{n}"), "uuid": uid(7) + "\n"},
    "uuid-missing": {k: v for k, v in call(file_path="{n}").items() if k != "uuid"},
    "time-newline": call(at="2026-10-07T18:29:03.104Z\n", file_path="{n}"),
    "time-seven-digits": call(at="2026-10-07T18:29:03.1040000Z", file_path="{n}"),
    "time-no-zone": call(at="2026-10-07T18:29:03", file_path="{n}"),
    "time-number": call(at=1791397743, file_path="{n}"),
    "tool-newline": call("Write\n", file_path="{n}"),
    "tool-unknown": call("Write2", file_path="{n}"),
    "not-assistant": {**call(file_path="{n}"), "type": "user"},
    "path-not-text": call(file_path=["{n}"]),
}


@pytest.mark.parametrize("name", list(BAD_RECORDS), ids=list(BAD_RECORDS))
def test_a_record_with_an_odd_id_time_tool_or_path_is_never_returned(home, name):
    wanted = note(home)
    record = json.loads(json.dumps(BAD_RECORDS[name]).replace("{n}", wanted.replace("\\", "\\\\")))
    make_log(home, [record, call(rec=9, file_path=wanted)])
    assert [w.record for w in find(home).writers] == [uid(9)]  # only the good record


def test_no_text_from_a_log_is_ever_returned(home):
    make_log(home, [{**call(file_path=note(home), content=SECRET, old_string=SECRET), "toolUseResult": {"content": SECRET}, "extra": SECRET}])
    result = find(home)
    assert len(result.writers) == 1 and SECRET not in repr(result)
    assert {f.name for f in fields(Writer)} == {"session", "record", "tool", "at"} and not any(str(home) in repr(w) for w in result.writers)


def test_damaged_lines_are_skipped_and_the_rest_is_still_searched(home):
    damaged = (b"not json at all\n", b'{"type": "tool_use"\n', b'\xff\xfe\xfd "tool_use"\n', b"[" * 200_000 + b' "tool_use"\n', b"\n")
    make_log(home, [call(file_path=note(home))], raw=damaged)
    result = find(home)
    assert len(result.writers) == 1 and result.complete


def test_a_line_too_long_to_read_is_skipped_and_the_result_says_it_is_incomplete(home, monkeypatch):
    monkeypatch.setattr(pv, "MAX_LINE_BYTES", 1000)
    huge = json.dumps(call("Write", rec=5, file_path=note(home), content="x" * 5000)).encode() + b"\n"
    make_log(home, [], raw=(huge, json.dumps(call(rec=6, file_path=note(home))).encode() + b"\n"))
    result = find(home)
    assert [w.record for w in result.writers] == [uid(6)] and not result.complete


def test_a_long_last_line_without_a_line_break_is_handled(home, monkeypatch):
    monkeypatch.setattr(pv, "MAX_LINE_BYTES", 1000)
    make_log(home, [call(file_path=note(home))], raw=(b'"tool_use" ' + b"x" * 5000,))
    result = find(home)
    assert len(result.writers) == 1 and not result.complete


def test_a_limit_on_logs_or_bytes_makes_the_result_incomplete(home, monkeypatch):
    make_log(home, [call(file_path=note(home))], name="a.jsonl")
    make_log(home, [call(rec=2, file_path=note(home))], name="b.jsonl")
    assert find(home).complete and find(home).logs_read == 2
    monkeypatch.setattr(pv, "MAX_LOG_FILES", 1)
    limited = find(home)
    assert limited.logs_read == 1 and not limited.complete
    monkeypatch.setattr(pv, "MAX_LOG_FILES", 500)
    monkeypatch.setattr(pv, "MAX_LOG_BYTES", 10)
    assert find(home).logs_read == 0 and not find(home).complete
    monkeypatch.setattr(pv, "MAX_LOG_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(pv, "MAX_TOTAL_BYTES", 10)
    assert find(home).logs_read == 0 and not find(home).complete


def test_too_many_project_folders_makes_the_result_incomplete(home, monkeypatch):
    make_log(home, [call(file_path=note(home))], project="-p2")
    monkeypatch.setattr(pv, "MAX_PROJECT_FOLDERS", 1)
    assert not find(home).complete


def test_only_the_newest_writers_are_returned_and_the_cap_is_reported(home):
    make_log(home, [call(rec=n, at=f"2026-10-07T18:29:{n:02d}.000Z", file_path=note(home)) for n in range(1, 26)])
    result = find(home)
    assert len(result.writers) == pv.MAX_WRITERS and not result.complete
    assert [w.at.second for w in result.writers] == list(range(25, 5, -1))  # newest first


def test_shell_commands_in_the_period_are_counted_never_read(home):
    make_log(home, [call("Bash", rec=1, command=SECRET), call("PowerShell", rec=2, command=SECRET),
                    call("Bash", rec=3, at="2026-10-07T19:00:00.000Z", command=SECRET)])
    result = find(home)
    assert result.shell_calls == 2 and result.writers == () and SECRET not in repr(result)


def test_a_log_not_written_since_the_period_began_is_never_opened(home, monkeypatch):
    make_log(home, [call(file_path=note(home))], name="old.jsonl", mtime=START - timedelta(days=1))
    make_log(home, [call(rec=2, file_path=note(home))], name="new.jsonl")
    opened = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(os.path.basename(str(p))), real(p, *a, **k))[1])
    result = find(home)
    assert opened == ["new.jsonl"] and [w.record for w in result.writers] == [uid(2)] and result.complete


def test_only_session_logs_inside_project_folders_are_opened(home, monkeypatch):
    make_log(home, [call(file_path=note(home))])
    projects = home / ".claude" / "projects"
    (projects / "stray.jsonl").write_text("{}\n", encoding="utf-8")
    (projects / "-p1" / "notes.md").write_text("not a log\n", encoding="utf-8")
    (projects / "-p1" / "sub").mkdir()
    (projects / "-p1" / "sub" / "deep.jsonl").write_text("{}\n", encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text("TOP-SECRET", encoding="utf-8")
    opened = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(os.path.basename(str(p))), real(p, *a, **k))[1])
    find(home)
    assert opened == ["s1.jsonl"]


@posix_only
def test_links_in_place_of_logs_folders_or_the_projects_root_are_never_followed(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    elsewhere = make_log(home, [call(file_path=note(home))], name="real.jsonl")
    os.rename(elsewhere, outside / "log.jsonl")
    os.symlink(outside / "log.jsonl", home / ".claude/projects/-p1/linked.jsonl")      # a log that is a link
    os.symlink(outside, home / ".claude/projects/-p2")                                  # a project folder that is a link
    assert find(home).writers == () and find(home).logs_read == 0
    os.rename(home / ".claude/projects", tmp_path / "moved")
    os.symlink(tmp_path / "moved", home / ".claude/projects")                           # the projects root is a link
    assert find(home).writers == () and find(home).logs_read == 0


def test_windows_junctions_are_never_followed_either(home, monkeypatch):
    make_log(home, [call(file_path=note(home))])
    monkeypatch.setattr(pv, "_is_reparse_point", lambda info: True)
    assert find(home).writers == () and find(home).logs_read == 0


def test_no_projects_folder_means_nothing_was_read(tmp_path):
    result = find_writers(tmp_path / "x.md", START, END, tmp_path / "empty-home")
    assert result == Provenance((), 0, 0, True)  # logs_read of 0 is how a caller tells "no logs" from "no writer found"


def test_an_unreadable_log_is_reported_not_raised(home, monkeypatch):
    make_log(home, [call(file_path=note(home))])
    def refuse(*a, **k):
        raise PermissionError("no")
    monkeypatch.setattr(os, "open", refuse)
    result = find(home)
    assert result.writers == () and not result.complete and result.logs_read == 0


def test_the_period_must_carry_a_time_zone(home):
    with pytest.raises(ValueError):
        find_writers(note(home), datetime(2026, 10, 7), END, home)
    with pytest.raises(ValueError):
        find_writers(note(home), START, datetime(2026, 10, 7, 19), home)
