## What this changes, and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was checked

Run these with the repository's own environment (see [CONTRIBUTING.md](https://github.com/juraj-jumic/memdebug/blob/main/CONTRIBUTING.md)):

- [ ] `python -m pytest -q -n auto`, `ruff check src tests`, `mypy` and `python -m memdebug selftest` all pass
- [ ] If this adds or changes a protection: I removed it on purpose and a test failed (CONTRIBUTING, ground rule 5). Otherwise: no protection changed
- [ ] Hostile input is handled: text read from a store, log or repository is bounded, shown through `safe_text` or the viewer's builder, and any name from the ledger is validated (validators end with `\Z`, never `$`) before it reaches a command or a path
- [ ] Windows is covered: file names that differ only by case, CRLF, and junctions are considered; a POSIX-only test is marked skipped with a reason

## Documents

- [ ] `CHANGELOG.md` has a line under `[Unreleased]` for anything a user would notice
- [ ] Anything the change makes untrue is updated, and a new limit is written into `docs/threat-model.md`

## Private data

- [ ] The diff and the commit messages contain no memory text, session transcripts, `*.db` files, `stores.json`, `backups/`, `copies/` or personal paths. Test data is made up
- [ ] I understand the history is public. A GitHub no-reply address is fine as the commit email

By contributing you agree that your contribution is licensed under the Apache License 2.0 (see [LICENSE](https://github.com/juraj-jumic/memdebug/blob/main/LICENSE)).
