"""Problems an independent security review found in the autostart plans before 1.0: Unicode quotes and non-ASCII names in the Windows script, `$` and control
characters in service files, the launcher's folder and the folder the watcher starts in. No real task, service or agent is ever created here."""
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import memdebug.autostart as au
import memdebug.cli as cli

runner = CliRunner()
SYSTEM32 = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32"
REAL_POWERSHELL = str(SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")
TOOLS = {"schtasks": "C:\\Win\\System32\\schtasks.exe", "powershell": REAL_POWERSHELL, "systemctl": "/usr/bin/systemctl", "launchctl": "/bin/launchctl"}
QUOTES = ["\u2018", "\u2019", "\u201a", "\u201b", "'"]


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(tuple(args))
        return (0, "")


def plan(system, command=None, args=None, tmp=None, **kw):
    return au.make_plan(system, command or ["/usr/bin/python3", "-m", "memdebug"], args or ["watch", "--log", "/d/w.log"], data_dir=tmp / "data", home=tmp / "home",
                        uid=501, tools=TOOLS, **kw)


@pytest.mark.parametrize("quote", QUOTES, ids=["u2018", "u2019", "u201a", "u201b", "ascii"])
def test_every_kind_of_single_quote_is_doubled_for_powershell(quote):
    word = "C:\\Users\\O" + quote + "Brien\\x.exe"
    assert au._powershell_quote(word) == "'C:\\Users\\O" + quote + quote + "Brien\\x.exe'"


def test_the_windows_script_carries_a_byte_order_mark_so_non_ascii_names_survive_powershell_5(tmp_path):
    p = plan("windows", ["C:\\Users\\Jos\u00e9\\python.exe", "-m", "memdebug"], tmp=tmp_path)
    au.install(p, Recorder())
    raw = p.files[0][0].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "Jos\u00e9".encode("utf-8") in raw


def test_only_the_powershell_script_gets_a_byte_order_mark(tmp_path):
    p = plan("linux", tmp=tmp_path)
    au.install(p, Recorder())
    assert not p.files[0][0].read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell's own parser")
def test_powershell_itself_reads_back_every_word_even_with_awkward_names(tmp_path):
    """The generated script is PARSED (not run) by the real PowerShell, and every quoted word must come back exactly as it went in."""
    words = ["C:\\Users\\O\u2019Brien \u2018x\u2019\\py thon.exe", "-m", "memdebug", "a'b", "Jos\u00e9 \u0112", "$(Get-Date)", "`n", "; calc"]
    data_dir = tmp_path / "d\u00e0ta \u2019x"
    p = au.make_plan("windows", words, [], data_dir=data_dir, home=tmp_path, tools=TOOLS)
    au.install(p, Recorder())
    probe = tmp_path / "probe.ps1"  # a parser probe of our own; it only reads the generated script
    probe.write_text("param($f)\n$e=$null;$t=$null\n$ast=[System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e)\n"
                     "$s=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst]},$true)|ForEach-Object{$_.Value}\n"
                     "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
                     "@{errors=@($e|ForEach-Object{$_.Message});strings=@($s)}|ConvertTo-Json -Compress -Depth 4\n", encoding="utf-8-sig")
    done = subprocess.run([REAL_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe), str(p.files[0][0])], capture_output=True)
    found = json.loads(done.stdout.decode("utf-8"))
    assert found["errors"] == []
    assert found["strings"] == ["Set-Location", str(data_dir), *words]  # the folder it starts in, then every word, untouched


def test_a_dollar_sign_in_a_path_is_not_expanded_by_systemd(tmp_path):
    p = plan("linux", ["/opt/$HOME/py"], ["watch", "--log", "/d/$x/w.log"], tmp=tmp_path)
    assert '"/opt/$$HOME/py"' in p.files[0][1] and '"/d/$$x/w.log"' in p.files[0][1]


BAD = ["a\nb", "a\rb", "a\x1b[2Jb", "a\x00b", "a\u2028b", "a\u2029b", "a\x85b", "a\x7fb"]


@pytest.mark.parametrize("bad", BAD, ids=["lf", "cr", "esc", "nul", "ls", "ps", "nel", "del"])
@pytest.mark.parametrize("system", ["windows", "linux", "macos"])
def test_a_control_character_anywhere_in_a_path_or_argument_refuses_the_plan(tmp_path, system, bad):
    with pytest.raises(au.AutostartError, match="control character"):
        plan(system, ["/usr/bin/python3", "-m", "memdebug"], ["watch", "--log", "/d/" + bad + "/w.log"], tmp=tmp_path)
    with pytest.raises(au.AutostartError, match="control character"):
        plan(system, ["/usr/bin/python3"], ["watch"], tmp=tmp_path / bad)


def test_a_launcher_folder_with_a_control_character_is_refused_too(tmp_path):
    with pytest.raises(au.AutostartError, match="control character"):
        au.make_plan("windows", ["x"], ["watch"], data_dir=tmp_path, home=tmp_path, tools=TOOLS, launcher_dir=tmp_path / "a\nb")


def test_the_unit_and_the_agent_start_in_the_data_folder_not_in_the_home_folder(tmp_path):
    unit = plan("linux", tmp=tmp_path).files[0][1]
    assert "WorkingDirectory=" + str(tmp_path / "data") + "\n" in unit
    data = plistlib.loads(plan("macos", tmp=tmp_path).files[0][1].encode("utf-8"))
    assert data["WorkingDirectory"] == str(tmp_path / "data")
    assert au._systemd_path("/a%b") == "/a%%b"


def test_the_windows_launcher_lives_where_asked_not_beside_a_ledger_in_a_shared_folder(tmp_path):
    shared, mine = tmp_path / "shared-drive", tmp_path / "my-profile"
    p = au.make_plan("windows", ["x"], ["watch"], data_dir=shared, home=tmp_path, tools=TOOLS, launcher_dir=mine)
    assert p.files[0][0] == mine / "memdebug-watch.ps1" and p.undo_remove == (mine / "memdebug-watch.ps1", mine / "memdebug-watch.xml")
    assert str(mine / "memdebug-watch.ps1") in p.files[1][1] and p.steps[0][5] == str(mine / "memdebug-watch.xml")
    assert au.make_plan("windows", ["x"], ["watch"], data_dir=shared, home=tmp_path, tools=TOOLS).files[0][0] == shared / "memdebug-watch.ps1"  # the default


def test_the_command_puts_the_windows_launcher_in_the_default_folder_even_with_a_ledger_elsewhere(tmp_path, monkeypatch):
    mine = tmp_path / "profile"
    monkeypatch.setenv("LOCALAPPDATA", str(mine))
    monkeypatch.setenv("XDG_DATA_HOME", str(mine))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(au, "system_name", lambda: "windows")
    monkeypatch.setattr(au, "default_run", Recorder())
    elsewhere = tmp_path / "shared" / "l.db"
    elsewhere.parent.mkdir()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("hello\n", encoding="utf-8")
    assert runner.invoke(cli.app, ["add", str(notes), "--name", "n", "--db", str(elsewhere)]).exit_code == 0
    assert runner.invoke(cli.app, ["autostart", "install", "--apply", "--yes", "--db", str(elsewhere)]).exit_code == 0
    launcher = mine / "memdebug" / "memdebug-watch.ps1"
    assert launcher.exists() and not (elsewhere.parent / "memdebug-watch.ps1").exists()
    text = launcher.read_text(encoding="utf-8-sig")
    assert str(elsewhere.resolve()) in text and "Set-Location -LiteralPath '" + str(elsewhere.resolve().parent) + "'" in text
