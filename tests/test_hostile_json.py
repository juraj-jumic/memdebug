"""Untrusted JSON nested thousands of levels deep must cost one record, never crash a reader (an independent review found several that did)."""
import json
import sqlite3

import pytest

import memdebug.laststatus as ls
import memdebug.stores as st
import memdebug.witness as wit
from memdebug.ledger import Ledger
from memdebug.textsafe import loads_untrusted
from test_ledger_compat import FIXTURES, load
from test_provenance_openwebui import BASE, make_db, source_of

DEEP = "[" * 200_000 + "]" * 200_000


def test_the_helper_parses_ordinary_json_and_turns_a_too_deep_document_into_a_value_error():
    assert loads_untrusted('{"a": [1, 2]}') == {"a": [1, 2]} and loads_untrusted(b"[true]") == [True]
    with pytest.raises(ValueError, match="nested too deeply"):
        loads_untrusted(DEEP)
    with pytest.raises(ValueError):
        loads_untrusted("{broken")  # the usual case still works as before


def test_a_status_record_nested_absurdly_deep_is_unusable_not_a_crash(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("[" * 90_000 + "]" * 90_000, encoding="utf-8")  # small enough to pass the size limit, so it really is parsed
    with pytest.raises(ls.StatusError):
        ls.read(path)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"], ids=["year1", "year9999"])
def test_a_time_at_the_edge_of_the_calendar_is_unusable_not_a_crash(tmp_path, stamp):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"version": 1, "checked_at": stamp, "ledger_ok": True, "stores": []}), encoding="utf-8")
    with pytest.raises(ls.StatusError, match="no usable time"):
        ls.read(path)


def test_a_settings_file_nested_absurdly_deep_is_a_clear_settings_error(tmp_path):
    path = tmp_path / "stores.json"
    path.write_text(DEEP, encoding="utf-8")
    with pytest.raises(st.SettingsError, match="damaged"):
        st.load_registry(path)


def test_an_open_webui_memory_with_deeply_nested_meta_is_listed_without_a_label(tmp_path):
    meta = "[" * 10_000 + "]" * 10_000  # under the 20 KB cap the reader allows for meta
    src = source_of(make_db(tmp_path / "w.db", memories=[("m1", "u1", "likes tea", meta, "user", BASE, BASE)]))
    assert src is None or src.actor_id is None  # no label could be read, and nothing crashed


def test_a_witness_file_with_a_deeply_nested_line_is_reported_as_a_problem(tmp_path):
    path = tmp_path / "witness.txt"
    path.write_text(DEEP + "\n", encoding="utf-8")  # far over the length a witness line may have, which is what stops it first
    ledger_path, _, _, _ = load(tmp_path, FIXTURES[-1])
    ledger = Ledger(ledger_path)
    check = wit.verify_witness(ledger, path)
    ledger.close()
    assert not check.ok and any("too long" in problem for problem in check.problems)
    short = tmp_path / "short.txt"
    short.write_text("[" * 900 + "]" * 900 + "\n", encoding="utf-8")  # within the length limit, deep enough to be a nuisance
    again = Ledger(ledger_path)
    assert not wit.verify_witness(again, short).ok
    again.close()


def test_a_ledger_entry_with_deeply_nested_text_fails_verification_instead_of_crashing_it(tmp_path):
    ledger_path, _, _, _ = load(tmp_path, FIXTURES[-1])
    db = sqlite3.connect(ledger_path)
    db.execute("UPDATE events SET payload = ? WHERE seq = 1", (DEEP,))
    db.commit()
    db.close()
    result = Ledger(ledger_path).verify()
    assert not result.ok and result.problems
