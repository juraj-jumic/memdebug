"""The stand-alone Windows program, opened by double-click: it explains itself and waits, and does so only then.

A person who double-clicks a command-line program sees a console window open and close at once. The rule that tells a double-click from a terminal was
measured with a real one-file build: 2 processes on the console in a window of its own, 4 from a shell.
"""
import io
import os
import sys

import pytest

from memdebug import standalone as sa

GOOD = {"frozen": True, "windows": True, "process_count": 2, "interactive": True}


def decide(argv=("memdebug.exe",), **changes):
    return sa.started_by_double_click(list(argv), **{**GOOD, **changes})


def test_a_console_of_its_own_with_no_arguments_is_a_double_click():
    assert decide() and decide(process_count=1)


@pytest.mark.parametrize("changes", [
    {"argv": ("memdebug.exe", "demo")}, {"argv": ("memdebug.exe", "--help")}, {"argv": ("memdebug.exe", "")},
    {"frozen": False}, {"windows": False}, {"interactive": False},
    {"process_count": 3}, {"process_count": 4}, {"process_count": 0}, {"process_count": None},
], ids=["demo", "help", "empty-arg", "not-standalone", "not-windows", "redirected", "three", "four", "none-attached", "unknown"])
def test_anything_else_is_not_a_double_click_and_runs_as_usual(changes):
    assert not decide(**changes)


def test_the_message_names_the_file_and_says_how_to_start_it(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sa, "console_process_count", lambda: 2)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "memdebug-windows-x64.exe"))  # a path in this system's own form, so the test runs everywhere
    monkeypatch.setattr(sa.os, "name", "nt")
    monkeypatch.setattr(sys, "stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr(sys, "stdout", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    waited = []
    assert sa.explain_if_double_clicked(["memdebug-windows-x64.exe"], wait=waited.append) is True
    shown = sys.stdout.getvalue()
    assert r".\memdebug-windows-x64.exe demo" in shown and "--help" in shown and "setup" in shown and "command-line program" in shown
    assert waited == ["Press Enter to close this window. "]  # it waited, so the window stays until the person has read it


def test_it_does_not_hang_when_there_is_no_input_to_wait_on(monkeypatch):
    monkeypatch.setattr(sa, "console_process_count", lambda: 2)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sa.os, "name", "nt")
    monkeypatch.setattr(sys, "stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr(sys, "stdout", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())

    def closed(prompt):
        raise EOFError

    assert sa.explain_if_double_clicked(["memdebug.exe"], wait=closed) is True


def test_a_normal_run_prints_nothing_and_waits_for_nothing(monkeypatch, capsys):
    def never(prompt):
        pytest.fail("must not wait")

    assert sa.explain_if_double_clicked(["memdebug.exe", "demo"], wait=never) is False  # arguments given
    assert sa.explain_if_double_clicked(["memdebug.exe"], wait=never) is False  # not a stand-alone build, and pytest's output is not a console
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name == "nt", reason="shows the answer where there is no console to ask")
def test_where_there_is_no_windows_console_the_count_is_unknown():
    assert sa.console_process_count() is None


@pytest.mark.skipif(os.name != "nt", reason="asks the Windows console")
def test_on_windows_the_count_is_asked_of_the_system_and_is_a_number_or_unknown():
    count = sa.console_process_count()
    assert count is None or count >= 0


def test_the_stand_alone_entry_point_uses_it_before_anything_else():
    from pathlib import Path

    entry = (Path(__file__).resolve().parents[1] / "packaging" / "entry.py")
    if not entry.exists():
        pytest.skip("run from a source checkout")
    text = entry.read_text(encoding="utf-8")
    assert "explain_if_double_clicked()" in text and text.index("explain_if_double_clicked()") < text.index("app()")
