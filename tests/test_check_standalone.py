"""`packaging/check_standalone.py`, the smoke test CI runs on every stand-alone build, must fail for the faults a stand-alone build has shown.

The stand-alone programs here are stubs: a script that prints what a build would print, started through a launcher.
"""
import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import pytest

import memdebug

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "check_standalone.py"
needs_repo = pytest.mark.skipif(not SCRIPT.exists(), reason="run from a source checkout")


def load():
    spec = importlib.util.spec_from_file_location("check_standalone_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_program(tmp_path, *, version=None, demo_exit=0, selftest_exit=0, status=None):
    """A stand-in stand-alone program. `status` maps a check name to what the self-test prints for it (default PASS)."""
    status = status or {}
    module = load()
    lines = [f"[{status.get(name, 'PASS')}] {name}: detail" for name in module.REQUIRED]
    stub = tmp_path / "stub.py"
    stub.write_text(textwrap.dedent(f"""
        import sys
        args = sys.argv[1:]
        if args == ["--version"]:
            print({version or 'memdebug ' + memdebug.__version__!r})
        elif args == ["demo"]:
            sys.exit({demo_exit})
        elif args == ["selftest"]:
            print({chr(10).join(lines)!r})
            sys.exit({selftest_exit})
        """), encoding="utf-8")
    if os.name == "nt":
        program = tmp_path / "stub-program.cmd"
        program.write_text(f'@"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8")
    else:
        program = tmp_path / "stub-program"
        program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
        program.chmod(0o755)
    return str(program)


@needs_repo
def test_a_good_build_passes(tmp_path, capsys):
    assert load().main(make_program(tmp_path)) == 0
    assert "stand-alone build OK" in capsys.readouterr().out


@needs_repo
@pytest.mark.parametrize("kwargs, expected", [
    ({"version": "memdebug 0.0.1"}, "--version printed"),
    ({"demo_exit": 1}, "demo exited 1"),
    ({"selftest_exit": 1}, "selftest exited 1"),
    ({"status": {"hostile repository config cannot run programs": "SKIP"}}, "'hostile repository config cannot run programs' is SKIP"),
    ({"status": {"rollback is safe": "SKIP"}}, "'rollback is safe' is SKIP"),
    ({"status": {"a hung process tree is killed": "FAIL"}}, "'a hung process tree is killed' is FAIL"),
], ids=["wrong-version", "demo-fails", "selftest-fails", "tripwire-skipped", "rollback-skipped", "stalled-fails"])
def test_each_fault_of_a_build_fails_the_check_and_says_which(tmp_path, capsys, kwargs, expected):
    assert load().main(make_program(tmp_path, **kwargs)) == 1
    assert expected in capsys.readouterr().err


@needs_repo
def test_a_check_missing_from_the_self_test_output_fails_too(tmp_path, capsys):
    program = make_program(tmp_path)
    stub = Path(tmp_path / "stub.py")
    stub.write_text(stub.read_text(encoding="utf-8").replace("[PASS] rollback is safe: detail", ""), encoding="utf-8")
    assert load().main(program) == 1
    assert "'rollback is safe' is missing" in capsys.readouterr().err
