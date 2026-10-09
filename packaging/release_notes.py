"""The release notes of one version: `python packaging/release_notes.py v0.5.0 [CHANGELOG.md]` prints that version's section of the changelog.

Used by the release workflow to fill the GitHub release. It fails, so the release stops, when the changelog has no section for the tag.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def notes_for(tag: str, changelog: str) -> str:
    """The text under the heading `## [X.Y.Z] - date` of the changelog, for the tag `vX.Y.Z`.

    Raises:
        ValueError: If the tag is not `vX.Y.Z` or the changelog has no non-empty section for it.
    """
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
    if match is None:
        raise ValueError(f"not a version tag: {tag!r}")
    section = re.search(rf"^## \[{re.escape(match.group(1))}\][^\n]*\n(.*?)(?=^## \[|\Z)", changelog, re.S | re.M)
    if section is None or not section.group(1).strip():
        raise ValueError(f"no changelog section for {tag}")
    return section.group(1).strip() + "\n"


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: python packaging/release_notes.py vX.Y.Z [CHANGELOG.md]")
    try:
        text = notes_for(sys.argv[1], Path(sys.argv[2] if len(sys.argv) == 3 else "CHANGELOG.md").read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        sys.exit(f"error: {exc}")
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # a console's own encoding must not change the notes
    sys.stdout.write(text)
