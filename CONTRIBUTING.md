# Contributing

Thanks for helping. memdebug is a security tool, so the bar for changes is "what happens if the input is hostile?".

## Setup

    pip install -e ".[dev]"
    pytest -n auto            # the whole suite, in parallel; plain `pytest` works too
    memdebug selftest         # proves the protections on THIS machine
    ruff check src tests      # lint (CI enforces it)
    mypy                      # types (CI enforces it)

You need Python 3.10+ and git 2.31+. The suite starts many git processes, so it is slower on Windows.

## Ground rules

1. **Everything read from a memory store or repository is untrusted.** Bound its size, never print it raw (use
   `safe_text`), and never let it become markup (use the viewer's builder, never string-built HTML).
2. **Adapters only read.** The one component allowed to change a memory store is `adapters/restore.py`, and it has its
   own rules: dry run first, exact bytes, a backup of anything git does not hold, no rewritten history, undo on failure.
3. **No shell, fixed argument lists, no programs named by the data.** git runs with a scrubbed environment and the
   repository's risky settings overridden. Do not add a git command without checking it cannot run a hook, filter or
   external helper.
4. **Explicit text encodings** everywhere (`encoding="utf-8"`); a test enforces this because Windows defaults to a legacy
   code page.
5. **A protection without a test that fails when it is removed is not a protection.** Break your own code on purpose
   (remove the check) and confirm a test notices. Several tests exist only because that exercise found a gap.
6. **Windows is a first-class platform.** Avoid POSIX-only assumptions; if a test truly needs POSIX, mark it and say why. Two traps that only show up
   there: file names ignore case ("A.db" and "a.db" are one file, so never name files after values that can differ only by case), and text-mode
   writes turn `\n` into `\r\n` (write bytes when exact content matters).
7. **Say what a feature does not do.** Add its limits to `docs/threat-model.md`.

## Adding a backend

Implement the `MemoryAdapter` protocol in `adapters/base.py` (`list_memories`, `read_history`, `history`), read-only,
validating every row. Look at `adapters/mem0.py` and `adapters/markdown_git.py` for the expected hardening, and add the
same kinds of tests (hostile rows, oversized values, odd encodings). A backend without a change history should record
what it observes instead of raising "outside the history" alarms.

## Adding an agent to the catalog

See `docs/agents.md`: a documented location (with a link), a test using a fake home folder, and watch only the named files when the
folder also holds credentials.

## Workflows and Dependabot

Dependabot proposes updates to the pinned actions in one grouped pull request a week. Let CI run on it, and for changes to the release
workflow also run its dry run (docs/releasing.md) from the pull request's branch before merging.

## Releases

See `docs/releasing.md`. A test keeps the version, the changelog, the licence files and the links in the documents in agreement.

## Pull requests

Keep them focused, include tests, update the docs that your change makes untrue, and run the whole suite. By
contributing you agree that your contribution is licensed under the Apache License 2.0 (see LICENSE).
