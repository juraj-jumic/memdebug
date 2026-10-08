# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [Semantic Versioning](https://semver.org/)
(alpha: anything may change before 1.0).

## [Unreleased]

### Added
- **Who wrote it.** For a changed note that looks suspicious, or that changed outside git, `memdebug check` and `memdebug watch` now also say which
  logged Claude Code session wrote it, from that agent's session logs ("who wrote it: ..."), or that no logged edit explains it. It returns only a
  session id, a time and a tool name, never a path or any text from a log. It is evidence, not proof: a deleted log looks the same as a note nobody
  wrote, and shell-made changes cannot be matched (they are only counted). See `docs/threat-model.md`.
- The viewer has an **Agents** page (`memdebug serve`): the known agents that appear to be installed and what memdebug could watch for each, with the command
  to run (`memdebug setup`). It only checks that folders exist, opens no file, and works even while the ledger is busy.
- Cline is in the agent catalog: `memdebug agents` and `memdebug setup` offer its global rules folder (`~/Documents/Cline/Rules`) once it holds markdown rules.

### Documentation
- The README has badges, a recorded demo (`docs/demo.gif`, with the unpaced recording in `docs/demo.cast`), and new "Why this matters", "How it works" and
  "Engineering" sections, checked against the code. There is now a pull request template and a bug-report form that tells people never to paste
  memory contents or session transcripts. CONTRIBUTING's second ground rule now names both rollback engines.

## [0.3.0] - 2026-10-07

### Added
- **Rollback for plain folders.** `memdebug rollback store NAME --to s1` restores a watched folder of notes (and, by the same command, git notes) to a
  snapshot: a dry run first, everything it replaces saved first byte for byte in a private folder next to the ledger, every write undone if a later
  step fails, and the rollback recorded in the ledger and itself undoable. A folder is rebuilt from the snapshot's text, so line endings are LF (or CRLF
  if the file it replaces uses CRLF throughout), and text a snapshot could not keep faithfully is skipped, never written.
- `memdebug selftest` has a new check, "folder rollback is safe", that proves the plain-folder rollback claims on your machine without needing git.
- `memdebug snapshot store NAME` saves a known-good copy of a watched store to roll back to. It refuses while the ledger holds an outside-history
  change or flagged wording since the store's last snapshot, until you add `--include-changes`.

### Changed
- **The viewer** shows how to put a store back: snapshot pages, the compare page, the page of a change made outside a store's history and the
  overview's "Needs a look" give the `memdebug rollback store` command as text to copy (the viewer itself still only reads). A store name from the
  ledger is only put into a command if it is safe to paste; for an outside change the snapshot offered is always one taken before it. A rollback of a
  plain folder is described as in place with a backup, not as a commit.
- The file-writing safety code of the git rollback engine (links, junctions, case clashes, atomic writes, undo) now lives in one shared module used by
  both engines. No behaviour change.
- `memdebug demo` now ends by pointing at `memdebug setup`, the guided way to watch your own agents, instead of an advanced command.
- Documentation: the README explains installing from PyPI, with a route that works on Windows without pipx.

## [0.2.0] - 2026-10-07

### Added
- **Witness:** a second copy of the ledger's head kept elsewhere, so a rewritten or cut-short ledger is noticed (`memdebug witness`, `verify --witness`).
- **Reports** in Markdown, JSON and SARIF, with CI exit codes (`memdebug report`).
- **Guided use:** `setup`, `add`, `stores`, `remove`, `check`, `status`, `watch`, `agents`, and a registry of watched stores.
- **More stores:** plain folders of markdown, Open WebUI's memory (read from a copy of its database, or copied by memdebug itself from a Docker
  container), and single-file memory for agents that keep credentials beside it (Gemini CLI, Codex CLI, Claude Code's global file).
- **An agent catalog** (Claude Code, OpenClaw, Gemini CLI, Codex CLI, Windsurf, Open WebUI) with locations taken from each agent's documentation.
- **Hints** ("worth a second look") for instructions to send data, remove confirmation or weaken safeguards, hidden characters and secret-like strings.
- **A first slice of provenance for Open WebUI:** the app's own `created_by` label and how close a chat was, as evidence only.
- `memdebug demo`, `memdebug --version`, Apache-2.0 licence, security policy, threat model, roadmap and contributing guide.

### Changed
- The release workflow re-downloads and re-checks the built files in a separate job before it can publish, so a dry run tests the whole hand-over.
- The viewer says plainly when a store keeps no history, instead of claiming every change went through one.
- Opaque ids (UUIDs) are shown short, with what the memory says next to them in `check`.
- Every pattern that validates an identifier now ends with a strict end-of-text anchor (a trailing newline used to slip through some of them).

### Fixed
- The witness reader accepts Windows line endings and a byte-order mark (git's autocrlf and some editors add them).
- Tests can no longer touch the Docker, home folder or data folder of the machine they run on.

## [0.1.0] - 2026-10-05

### Added
- Tamper-evident ledger with snapshots, `verify`, and compare; markdown/git and Mem0 stores; detection of changes that bypass a store's own history.
- A read-only browser viewer; rollback for markdown/git with backups, undo on failure and a recorded, undoable result; `selftest`.
