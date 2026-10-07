"""The witness: a second copy of the ledger's head that a rewritten or truncated ledger cannot match."""
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
from memdebug import witness as w
from memdebug.ledger import Ledger
from memdebug.models import MemoryEvent, Op

runner = CliRunner()
T = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")


def make_ledger(path, texts):
    ledger = Ledger(path)
    for i, text in enumerate(texts):
        ledger.append(MemoryEvent(backend="fake", memory_id=f"m{i}", op=Op.ADD, ts=T, scope={"user_id": "u"}, after=text))
    return ledger


@pytest.fixture
def ledger(tmp_path):
    return make_ledger(tmp_path / "l.db", ["one", "two", "three"])


def test_a_witnessed_ledger_verifies_and_counts_what_is_newer(ledger, tmp_path):
    path = tmp_path / "w.txt"
    line, new = w.append_witness(ledger, path, now=T)
    assert new and line.seq == 3 and line.head == ledger.entries()[-1].hash and line.prev == w.GENESIS
    assert w.verify_witness(ledger, path).ok
    ledger.append(MemoryEvent(backend="fake", memory_id="m9", op=Op.ADD, ts=T, scope={"user_id": "u"}, after="later"))
    check = w.verify_witness(ledger, path)
    assert check.ok and check.unwitnessed == 1 and check.last_seq == 3


def test_witnessing_twice_without_changes_adds_nothing_and_later_lines_chain(ledger, tmp_path):
    path = tmp_path / "w.txt"
    first, _ = w.append_witness(ledger, path, now=T)
    again, new = w.append_witness(ledger, path, now=T)
    assert not new and again == first and len(path.read_text().splitlines()) == 1
    ledger.append(MemoryEvent(backend="fake", memory_id="m9", op=Op.ADD, ts=T, scope={"user_id": "u"}, after="later"))
    second, new = w.append_witness(ledger, path, now=T)
    assert new and second.prev == first.digest and len(path.read_text().splitlines()) == 2
    assert w.verify_witness(ledger, path).ok


def test_a_rewritten_ledger_cannot_match_the_witness(ledger, tmp_path):
    path = tmp_path / "w.txt"
    w.append_witness(ledger, path, now=T)
    forged = make_ledger(tmp_path / "forged.db", ["one", "TWO (edited)", "three"])  # a perfectly valid chain, different content
    assert forged.verify().ok
    check = w.verify_witness(forged, path)
    assert not check.ok and "rewritten" in check.problems[0]


def test_a_truncated_ledger_cannot_match_the_witness_even_though_its_own_chain_is_fine(ledger, tmp_path):
    path = tmp_path / "w.txt"
    w.append_witness(ledger, path, now=T)
    ledger.close()
    copy = tmp_path / "cut.db"
    shutil.copyfile(tmp_path / "l.db", copy)
    raw = sqlite3.connect(copy)
    raw.execute("DELETE FROM events WHERE seq = 3")
    raw.commit()
    raw.close()
    cut = Ledger(copy)
    # (the ledger's own chain cannot tell that its newest entry is gone: only the witness can)
    check = w.verify_witness(cut, path)
    assert not check.ok and "removed" in check.problems[0]


def lines_of(path):
    return path.read_text().splitlines()


def build_chain(ledger, path, times=3):
    for i in range(times):
        ledger.append(MemoryEvent(backend="fake", memory_id=f"x{i}", op=Op.ADD, ts=T, scope={"user_id": "u"}, after=f"x{i}"))
        w.append_witness(ledger, path, now=T)


@pytest.mark.parametrize("damage", ["edit_head", "delete_middle", "swap", "extra_space", "garbage_tail", "blank_line", "not_json", "bad_utf8",
                                    "huge_line", "wrong_keys", "future_version", "negative_seq"])
def test_a_damaged_witness_file_is_reported_and_never_extended(ledger, tmp_path, damage):
    path = tmp_path / "w.txt"
    build_chain(ledger, path)
    ls = lines_of(path)
    if damage == "edit_head":
        data = json.loads(ls[1]); data["head"] = "0" * 64
        ls[1] = w._line_text(data["seq"], data["head"], data["count"], data["at"], data["prev"])
    elif damage == "delete_middle":
        del ls[1]
    elif damage == "swap":
        ls[0], ls[1] = ls[1], ls[0]
    elif damage == "extra_space":
        ls[1] = ls[1].replace(":", ": ", 1)
    elif damage == "garbage_tail":
        ls.append("this is not a witness line")
    elif damage == "blank_line":
        ls.insert(1, "")
    elif damage == "not_json":
        ls[2] = "{"
    elif damage == "huge_line":
        ls[2] = "x" * 5000
    elif damage == "wrong_keys":
        ls[2] = json.dumps({"v": 1, "seq": 1})
    elif damage == "future_version":
        ls[2] = ls[2].replace('"v":1', '"v":2')
    elif damage == "negative_seq":
        ls[2] = ls[2].replace('"seq":5', '"seq":-5').replace('"seq":4', '"seq":-4').replace('"seq":6', '"seq":-6')
    if damage == "bad_utf8":
        path.write_bytes(b"\xff\xfe not text\n")
    else:
        path.write_text("\n".join(ls) + "\n")
    check = w.verify_witness(ledger, path)
    assert not check.ok and check.problems
    before = path.read_bytes()
    with pytest.raises(w.WitnessError, match="damaged"):
        w.append_witness(ledger, path, now=T)
    assert path.read_bytes() == before


def test_an_empty_or_missing_witness_is_not_a_pass(ledger, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    check = w.verify_witness(ledger, empty)
    assert not check.ok and "empty" in check.problems[0]
    with pytest.raises(w.WitnessError, match="does not exist"):
        w.verify_witness(ledger, tmp_path / "nope.txt")


def test_the_witness_path_must_be_a_plain_file_in_an_existing_folder(ledger, tmp_path):
    with pytest.raises(w.WitnessError, match="folder"):
        w.append_witness(ledger, tmp_path / "missing" / "w.txt")
    folder = tmp_path / "adir"
    folder.mkdir()
    with pytest.raises(w.WitnessError, match="plain file"):
        w.append_witness(ledger, folder)
    with pytest.raises(w.WitnessError, match="ledger is empty"):
        w.append_witness(Ledger(tmp_path / "blank.db"), tmp_path / "w.txt")


@posix_only
def test_a_link_is_never_followed_or_written_through(ledger, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    link = tmp_path / "w.txt"
    os.symlink(victim, link)
    with pytest.raises(w.WitnessError, match="plain file"):
        w.append_witness(ledger, link)
    with pytest.raises(w.WitnessError, match="plain file"):
        w.verify_witness(ledger, link)
    assert victim.read_text() == "keep me\n"


def test_the_witness_holds_hashes_and_counts_only(ledger, tmp_path):
    path = tmp_path / "w.txt"
    make = make_ledger(tmp_path / "secret.db", ["TOP-SECRET memory text", "another secret"])
    w.append_witness(make, path, now=T)
    assert "secret" not in path.read_text().lower() and "memory text" not in path.read_text()


def test_the_same_disk_warning_appears_when_it_should(ledger, tmp_path):
    path = tmp_path / "w.txt"
    w.append_witness(ledger, path, now=T)
    check = w.verify_witness(ledger, path, ledger_path=tmp_path / "l.db")
    assert check.ok and check.warnings and "same disk" in check.warnings[0]
    assert not w.verify_witness(ledger, path).warnings  # no ledger path given: nothing to compare


# -- the commands ------------------------------------------------------------------------------------------------

def test_the_commands_witness_and_check(tmp_path):
    db = tmp_path / "l.db"
    make_ledger(db, ["a", "b"]).close()
    file = tmp_path / "w.txt"
    result = runner.invoke(cli.app, ["witness", "--file", str(file), "--db", str(db)])
    assert result.exit_code == 0 and "Witnessed: entry 2 of 2" in result.output
    again = runner.invoke(cli.app, ["witness", "--file", str(file), "--db", str(db)])
    assert "Already witnessed" in again.output
    ok = runner.invoke(cli.app, ["verify", "--db", str(db), "--witness", str(file)])
    assert ok.exit_code == 0 and "Ledger intact." in ok.output and "Witness agrees" in ok.output


def test_verify_fails_when_the_witness_disagrees_and_witness_refuses_a_broken_ledger(tmp_path):
    db = tmp_path / "l.db"
    make_ledger(db, ["a", "b"]).close()
    file = tmp_path / "w.txt"
    runner.invoke(cli.app, ["witness", "--file", str(file), "--db", str(db)])
    other = tmp_path / "other.db"
    make_ledger(other, ["a", "CHANGED"]).close()
    bad = runner.invoke(cli.app, ["verify", "--db", str(other), "--witness", str(file)])
    assert bad.exit_code == 1 and "rewritten" in bad.output
    raw = sqlite3.connect(other)
    raw.execute("UPDATE events SET payload = replace(payload, 'CHANGED', 'tampered') WHERE seq = 2")
    raw.commit()
    raw.close()
    refused = runner.invoke(cli.app, ["witness", "--file", str(tmp_path / "w2.txt"), "--db", str(other)])
    assert refused.exit_code == 2 and "Nothing was witnessed" in refused.output and not (tmp_path / "w2.txt").exists()


def test_reformatting_only_the_last_line_is_still_noticed(ledger, tmp_path):
    path = tmp_path / "w.txt"
    w.append_witness(ledger, path, now=T)
    path.write_text(path.read_text().replace(":", ": ", 1))  # still valid JSON, same values, but not the form memdebug writes
    check = w.verify_witness(ledger, path)
    assert not check.ok and "differs" in check.problems[0]


def test_a_witness_that_goes_back_in_time_is_noticed_even_if_its_chain_is_valid(ledger, tmp_path):
    entries = ledger.entries()
    path = tmp_path / "w.txt"
    first = w._line_text(3, entries[2].hash, 3, "2026-10-05T12:00:00Z", w.GENESIS)
    digest = w.WitnessLine(3, entries[2].hash, 3, "2026-10-05T12:00:00Z", w.GENESIS, first).digest
    second = w._line_text(2, entries[1].hash, 3, "2026-10-05T12:05:00Z", digest)  # a newer line about an OLDER entry
    path.write_bytes((first + "\n" + second + "\n").encode("utf-8"))  # bytes: text mode would turn \n into \r\n on Windows
    check = w.verify_witness(ledger, path)
    assert not check.ok and "back in time" in check.problems[0]


def test_a_windows_style_reparse_point_is_treated_as_a_link(ledger, tmp_path, monkeypatch):
    path = tmp_path / "w.txt"
    w.append_witness(ledger, path, now=T)
    monkeypatch.setattr(w, "_is_reparse_point", lambda info: True)
    with pytest.raises(w.WitnessError, match="plain file"):
        w.append_witness(ledger, path)
    with pytest.raises(w.WitnessError, match="plain file"):
        w.verify_witness(ledger, path)


def three_line_witness(ledger, path):
    for i in range(3):
        ledger.append(MemoryEvent(backend="fake", memory_id=f"y{i}", op=Op.ADD, ts=T, scope={"user_id": "u"}, after=f"y{i}"))
        w.append_witness(ledger, path, now=T)
    return path.read_bytes()


def test_windows_line_endings_and_a_byte_order_mark_do_not_look_like_tampering(ledger, tmp_path):
    """Git's autocrlf and some editors change these; the chain hashes each line's text, so they mean nothing."""
    path = tmp_path / "w.txt"
    raw = three_line_witness(ledger, path)
    for name, variant in (("crlf", raw.replace(b"\n", b"\r\n")), ("bom", b"\xef\xbb\xbf" + raw), ("both", b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")),
                          ("no-final-newline", raw.rstrip(b"\n"))):
        path.write_bytes(variant)
        check = w.verify_witness(ledger, path)
        assert check.ok and check.lines == 3, name


def test_a_witness_that_was_converted_can_still_be_extended_and_checked(ledger, tmp_path):
    path = tmp_path / "w.txt"
    path.write_bytes(three_line_witness(ledger, path).replace(b"\n", b"\r\n"))
    ledger.append(MemoryEvent(backend="fake", memory_id="z", op=Op.ADD, ts=T, scope={"user_id": "u"}, after="z"))
    line, new = w.append_witness(ledger, path, now=T)
    assert new and w.verify_witness(ledger, path).ok and w.verify_witness(ledger, path).lines == 4  # mixed endings are fine


def test_other_whitespace_changes_are_still_tampering(ledger, tmp_path):
    path = tmp_path / "w.txt"
    raw = three_line_witness(ledger, path)
    for name, variant in (("lone-cr", raw.replace(b"\n", b"\r", 1)), ("trailing-space", raw.replace(b"\n", b" \n", 1)),
                          ("tab", b"\t" + raw), ("double-bom", b"\xef\xbb\xbf\xef\xbb\xbf" + raw)):
        path.write_bytes(variant)
        assert not w.verify_witness(ledger, path).ok, name
