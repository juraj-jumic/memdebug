# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [Semantic Versioning](https://semver.org/)
(alpha: anything may change before 1.0).

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
