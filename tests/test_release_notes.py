"""`packaging/release_notes.py`: the release workflow fills the GitHub release from the changelog, and must stop if the changelog has no section."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "release_notes.py"
needs_repo = pytest.mark.skipif(not SCRIPT.exists(), reason="run from a source checkout")

CHANGELOG = """# Changelog

## [Unreleased]

- not yet

## [0.5.0] - 2026-10-09

### Added
- the new thing

## [0.4.1] - 2026-10-09

### Fixed
- an old thing
"""


def load():
    spec = importlib.util.spec_from_file_location("release_notes_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_repo
def test_the_notes_are_exactly_the_versions_own_section():
    notes = load().notes_for("v0.5.0", CHANGELOG)
    assert notes == "### Added\n- the new thing\n"
    assert "old thing" not in notes and "not yet" not in notes


@needs_repo
def test_the_last_section_runs_to_the_end_of_the_file():
    assert load().notes_for("v0.4.1", CHANGELOG) == "### Fixed\n- an old thing\n"


@needs_repo
@pytest.mark.parametrize("tag", ["v9.9.9", "0.5.0", "v0.5", "v0.5.0-rc1", "v0.5.0\n", "vX.Y.Z", ""], ids=["missing", "no-v", "short", "suffix", "newline", "letters", "empty"])
def test_a_tag_with_no_section_or_not_a_version_stops_the_release(tag):
    with pytest.raises(ValueError):
        load().notes_for(tag, CHANGELOG)


@needs_repo
def test_an_empty_section_stops_the_release():
    with pytest.raises(ValueError, match="no changelog section"):
        load().notes_for("v0.5.0", "## [0.5.0] - 2026-10-09\n\n## [0.4.1] - 2026-10-09\n- x\n")


@needs_repo
def test_the_real_changelog_has_a_section_for_the_current_version():
    import memdebug

    assert load().notes_for(f"v{memdebug.__version__}", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")).strip()


@needs_repo
def test_the_command_prints_the_notes_and_fails_loudly_for_a_missing_section(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    log.write_text(CHANGELOG, encoding="utf-8")
    good = subprocess.run([sys.executable, str(SCRIPT), "v0.5.0", str(log)], capture_output=True, text=True)
    assert good.returncode == 0 and good.stdout == "### Added\n- the new thing\n"
    bad = subprocess.run([sys.executable, str(SCRIPT), "v1.0.0", str(log)], capture_output=True, text=True)
    assert bad.returncode != 0 and bad.stdout == "" and "no changelog section" in bad.stderr
