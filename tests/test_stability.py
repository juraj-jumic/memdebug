"""What docs/stability.md promises is pinned here, so breaking it is a decision somebody has to make on purpose and not an accident.

Changing a pinned value below means a major version (or, for something that is only added, a minor one): update docs/stability.md and the
changelog in the same change."""
import io
import json
import re
import runpy
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from typer.testing import CliRunner

import memdebug.cli as cli
import memdebug.report as rp
import memdebug.stores as st
import memdebug.witness as wit
from memdebug.backups import _STAMP
from test_ledger_compat import FIXTURES, load

DOC = (Path(__file__).parent.parent / "docs" / "stability.md").read_text(encoding="utf-8")
runner = CliRunner()


def test_the_example_in_the_document_checks_every_real_ledger_without_memdebug(tmp_path, monkeypatch):
    code = re.search(r"```python\n(.*?)```", DOC, re.S).group(1)
    script = tmp_path / "check.py"
    script.write_text(code, encoding="utf-8")
    for fixture in FIXTURES:
        folder = tmp_path / fixture.stem.split("-")[1]
        folder.mkdir()
        path, _, _, events = load(folder, fixture)
        monkeypatch.setattr(sys, "argv", [str(script), str(path)])
        out = io.StringIO()
        with redirect_stdout(out):
            runpy.run_path(str(script), run_name="__main__")
        assert f"{events} entries, the chain is intact" in out.getvalue()


def test_the_example_in_the_document_notices_a_changed_entry(tmp_path, monkeypatch):
    import sqlite3
    code = re.search(r"```python\n(.*?)```", DOC, re.S).group(1)
    script = tmp_path / "check.py"
    script.write_text(code, encoding="utf-8")
    path, _, _, _ = load(tmp_path, FIXTURES[-1])
    db = sqlite3.connect(path)
    db.execute("UPDATE events SET payload = replace(payload, 'likes tea', 'likes TEA') WHERE payload LIKE '%likes tea%'")
    db.commit()
    db.close()
    monkeypatch.setattr(sys, "argv", [str(script), str(path)])
    with pytest.raises(AssertionError, match="content was changed"):
        runpy.run_path(str(script), run_name="__main__")


def test_the_json_report_of_an_old_ledger_carries_the_schema_version(tmp_path):
    from memdebug.ledger import Ledger
    path, _, _, _ = load(tmp_path, FIXTURES[-1])
    ledger = Ledger(path)
    data = json.loads(rp.to_json(rp.build_report(ledger)))
    ledger.close()
    assert rp.SCHEMA_VERSION == 1 and data["schema_version"] == 1 and data["tool"]["name"] == "memdebug" and data["integrity"]["ok"] is True


def test_the_report_rule_ids_are_pinned():
    assert set(rp.RULES) == {"outside-history", "untrusted-source", "integrity", "hint", "witness"}


def test_the_witness_line_format_is_pinned():
    line = wit._line_text(3, "a" * 64, 3, "2026-10-05T12:00:00Z", "b" * 64)
    assert json.loads(line) == {"v": 1, "seq": 3, "head": "a" * 64, "count": 3, "at": "2026-10-05T12:00:00Z", "prev": "b" * 64}
    assert line == '{"at":"2026-10-05T12:00:00Z","count":3,"head":"' + "a" * 64 + '","prev":"' + "b" * 64 + '","seq":3,"v":1}'


def test_the_backup_folder_name_is_pinned():
    assert _STAMP.match("20261005T120000Z-0123abcd") and not _STAMP.match("2026-10-05-0123abcd")


def test_the_settings_file_is_version_1_and_refuses_what_it_does_not_know(tmp_path):
    registry = st.Registry([st.StoreConfig("n", "folder", "/x")])
    path = tmp_path / "stores.json"
    st.save_registry(registry, path)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    for bad in ({"version": 2, "stores": []}, {"version": 1, "stores": [], "surprise": True},
                {"version": 1, "stores": [{"name": "n", "kind": "folder", "path": "/x", "surprise": 1}]}):
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(st.SettingsError):
            st.load_registry(path)


def test_the_documented_commands_all_exist():
    names = {c.name or c.callback.__name__ for c in cli.app.registered_commands} | {g.name for g in cli.app.registered_groups}
    promised = {"timeline", "verify", "report", "witness", "diff", "where", "selftest", "demo", "add", "stores", "remove", "check", "status",
                "watch", "agents", "setup", "serve", "sync", "snapshot", "rollback", "backups", "autostart"}
    assert promised <= {n.replace("_command", "").replace("_", "-") for n in names}


@pytest.mark.parametrize("args, code", [
    (["add", "{missing}", "--db", "{empty}"], 2),     # an error memdebug understands (a path that is not there)
    (["stores", "--db", "{empty}"], 0),
    (["watch", "--once", "--db", "{empty}"], 2),      # nothing to watch yet
])
def test_exit_codes_for_the_simplest_cases(tmp_path, args, code):
    empty = tmp_path / "l.db"
    values = {"{missing}": str(tmp_path / "nothing.db"), "{empty}": str(empty)}
    assert runner.invoke(cli.app, [values.get(a, a) for a in args]).exit_code == code


def test_a_failed_verification_exits_1_and_an_unexpected_error_70(tmp_path, monkeypatch):
    import sqlite3
    path, _, _, _ = load(tmp_path, FIXTURES[-1])
    db = sqlite3.connect(path)
    db.execute("UPDATE events SET payload = replace(payload, 'likes tea', 'likes TEA') WHERE payload LIKE '%likes tea%'")
    db.commit()
    db.close()
    assert runner.invoke(cli.app, ["verify", "--db", str(path)]).exit_code == 1
    monkeypatch.delenv("MEMDEBUG_DEBUG", raising=False)
    monkeypatch.setattr(cli, "default_ledger_path", lambda: (_ for _ in ()).throw(ZeroDivisionError("boom")))
    assert runner.invoke(cli.app, ["where"]).exit_code == 70


def test_every_link_in_the_document_points_at_a_file_that_exists():
    root = Path(__file__).parent.parent / "docs"
    for target in re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", DOC):
        if not target.startswith("http"):
            assert (root / target).resolve().exists(), target
