# Contributing

Thanks for helping. memdebug is a security tool, so the bar for changes is "what happens if the input is hostile?".

## Private data comes first

memdebug reads people's agent memory, so this repository must never hold any of it. Never commit, attach or paste (in an issue, a pull
request, a test or a commit message): real memory text, session transcripts, a ledger or any other `*.db` file, `stores.json`, backup or copy
folders, the link `memdebug serve` prints (it contains a secret), tokens or keys, or paths that name your user account. Test data is made up.
The ignore file keeps the common cases out (databases, `stores.json`, copy and backup folders, `*.jsonl` session logs, reports, keys), but it
is only a safety net: read `git status` and `git diff --cached` before every commit. The history is public, and a pushed commit cannot be
taken back without rewriting it.

## Reporting a security problem

Do not open a public issue or pull request for a security problem. Report it privately through GitHub
(https://github.com/juraj-jumic/memdebug/security/advisories/new); [SECURITY.md](SECURITY.md) says what counts and what to include. Keep private
text out of the report as well: a made-up example that behaves the same way is enough.

## Setup

    python -m venv .venv
    .venv\Scripts\activate           # Windows (PowerShell or cmd); on macOS and Linux: source .venv/bin/activate
    pip install -e ".[dev]"
    python -m pytest -n auto         # the whole suite, in parallel; plain `python -m pytest` works too
    python -m memdebug selftest      # proves the protections on THIS machine
    ruff check src tests             # lint (CI enforces it)
    mypy                             # types (CI enforces it)

You need Python 3.10+ and git 2.31+. The suite starts many git processes, so it is slower on Windows. Run everything through this environment's
`python -m ...`: a `memdebug` command on your PATH (a pipx install, say) can be an older release than the code you are editing, while
`python -m memdebug` is always your checkout. Two release-hygiene tests are skipped unless PyYAML is installed (`pip install pyyaml`).

## Ground rules

1. **Everything read from a memory store or repository is untrusted.** Bound its size, never print it raw (use
   `safe_text`), and never let it become markup (use the viewer's builder, never string-built HTML).
2. **Adapters only read.** The only code allowed to change a memory store is the two rollback engines, `adapters/restore.py` (git
   notes) and `adapters/folder_restore.py` (plain folders), which share the file-writing code in `adapters/fileops.py`. They have their
   own rules: a dry run first, a plan the person confirms, a backup of anything not already held (in git, or in a private backup folder),
   no rewritten history, undo on failure.
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

## Writing tests

* Use made-up data only. Every test already gets an empty home folder and no Docker (`isolated_home` and `no_real_docker` in
  `tests/conftest.py`), so never read a real home folder, agent folder, session log or container, and never put a real transcript in a
  fixture. Write a made-up log, as `tests/test_provenance.py` does. A test that sets the home folder sets both `HOME` and `USERPROFILE`.
* A new protection needs a test that fails when it is removed (ground rule 5). A new validator or route pattern ends with `\Z`, never `$`,
  and is added to `test_no_validator_accepts_a_trailing_line_break` in `tests/test_docker_source.py`, which tries every pattern with a
  trailing line break.
* Give parametrized tests short `ids=`, and mark a POSIX-only test skipped with a reason (see `posix_only` in `tests/test_provenance.py`).

## Changing the viewer

The viewer (`memdebug serve`) only looks. It answers GET and HEAD only, sends no JavaScript, and cannot change a store or the ledger (the
theme switch sets one cookie and nothing else). Pages are built with the escaping builder in `src/memdebug/viewer/html.py`, never by joining
strings. The only form is the Compare picker, which uses `method="get"` and just navigates; a test fails if any form uses another method.
Where a person may want to act, show the command as text to paste, as the "put it back" guidance does, and build it only from names that pass
`valid_name`. A new page needs a route anchored with `\Z`, an entry in the trailing-line-break test, and an entry in `ALL_PAGES` in
`tests/test_viewer.py`. What is defended, and what is not: [docs/threat-model.md](docs/threat-model.md#the-viewer).

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

Keep them focused (one topic), include tests, update the docs that your change makes untrue, and run the whole suite; the pull request
template lists what to check. Add a line under `[Unreleased]` in `CHANGELOG.md` for anything a user would notice, and write a new limit into
`docs/threat-model.md`. Do not bump the version or rename the changelog's heading: that is part of a release. The required checks must pass
before a pull request can merge, and only the maintainer creates release tags (the maintainer can bypass both rules, so they guard against
mistakes, not against the maintainer). Commits stay public for good, so use a GitHub no-reply address as your commit email if you would
rather not publish your own. By contributing you agree that your contribution is licensed under the Apache License 2.0 (see LICENSE).
