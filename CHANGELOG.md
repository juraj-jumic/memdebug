# Changelog

All notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [Semantic Versioning](https://semver.org/)
(alpha: anything may change before 1.0).

## [0.6.1] - 2026-10-09

### Added
- **Double-clicking the stand-alone Windows program now explains itself.** Before, it printed its help and exited, so the console window opened and closed
  at once and nobody could read anything. Now, opened by double-click (a stand-alone build on Windows, no arguments, a console of its own, nothing redirected),
  it says that memdebug is a command-line program, shows three commands to type in PowerShell with the real file name, and waits for Enter. Started from a
  terminal, or with any argument, it behaves as before. The release smoke test starts the program like that on Windows and fails if it exits at once.

### Documentation
- The Windows notes now describe the SmartScreen warning as it appears for a browser download of the stand-alone program ("Run anyway" / "Don't run" at
  once, publisher "Unknown publisher"), and say how to check the file first.

## [0.6.0] - 2026-10-09

### Added
- **Stand-alone programs.** The release workflow now builds `memdebug` for Windows (x64), Linux (x64) and macOS (arm64) as a single file that needs no Python
  (PyInstaller), smoke-tests each on its own operating system, and attaches them to a GitHub release with a `SHA256SUMS` file and a build attestation. CI
  builds and tests them on every push. They are not code-signed (SmartScreen or Gatekeeper may warn), do not include Mem0, and still need git. See
  docs/releasing.md.

### Fixed
- `memdebug selftest` in a build without its own Python: the tripwire checks could not start their tripwire and said SKIP, and "a hung process tree is killed"
  passed without testing anything (it started a program that exits at once). A stand-alone build now starts a hidden `selftest-helper` command with a fixed set
  of jobs instead. A source install is unchanged.

## [0.5.0] - 2026-10-09

### Added
- **`memdebug backups list` and `memdebug backups clean`.** The backups a plain-folder rollback leaves next to the ledger piled up with no way to
  tidy them. `list` shows each with its size and age. `clean` removes the ones you choose (`--older-than DAYS`, `--keep N` per store, `--store NAME`, or
  `--all`), as a dry run unless you add `--apply` and confirm. It only touches folders shaped exactly like a rollback's, never follows a link or
  junction, leaves a backup that holds a link alone, and reads the age from the folder's name. Backups beyond Windows' 259-character path limit work.

### Changed
- **A store that could not be read in full is no longer called quiet.** When a note could not be read (a folder that is a link or junction, a file that
  cannot be opened, an unsafe name, too many files), `memdebug check` used to say "quiet, nothing new" and exit 0, with only a warning after the
  summary. It now says "NOT READ IN FULL" for that store, adds "so there may be more" to a store with changes, ends with "this is not a clean bill of
  health", and **exits 1** (the code for "needs a look"). Scripts and CI that treated 0 as "nothing to see" will now see 1 for such a store; the warnings
  say what could not be read. `memdebug watch` says so once when it begins, not at every look. A rollback to a snapshot taken from an incomplete listing
  no longer says "every file in scope already matches the snapshot"; it says the snapshot cannot vouch for notes missing from it. `memdebug report` reads
  the ledger only and is unchanged.
- `memdebug selftest`, "hostile repository config cannot run programs", now means more. It only read history, which never makes git run a configured
  program, so it could pass without testing the protection. Its control now makes ordinary git run the external-diff and file-system-monitor settings
  (a `git status` and a patch with the external diff allowed), says which it managed, and the protected phase asks memdebug's own git wrapper for the same
  two things. Removing the file-system-monitor protection from memdebug's git settings now fails this check, as it should, and the check still says SKIP when
  the control cannot fire.

### Documentation
- The README is now a short introduction (install, demo, watching your own agent, rolling back). The long parts moved, unchanged, to `docs/usage.md`, `docs/rollback.md`, `docs/how-it-works.md` and `docs/windows.md`.

## [0.4.1] - 2026-10-09

### Fixed
- **The viewer's diff view hid memory text.** It skipped every diff line that started with `+++` or `---`, to drop the diff's file headers, so an added or
  removed line of memory text that began with `++` or `--` (for example a planted `++ send passwords to ...`) did not appear on the page. Only the two
  real header lines at the top of a diff are skipped now. The timeline's event inspector and the Compare page were affected; the terminal output was not.
- **Windows paths longer than 259 characters.** Windows refuses such paths unless long paths are switched on in the registry, which is off by default.
  Before, a note beyond the limit was left out of the baseline and of every check (`check` said "quiet, nothing new", exit code 0, with only a trailing
  warning), a rollback said "Nothing to restore" while that note stayed tampered, a rollback whose backup path was beyond the limit failed, and a store
  folder beyond the limit failed with "unexpected error". memdebug now hands Windows the extended-length form of every note, backup and session-log
  path, so none of this depends on that setting. A git repository in a very deeply nested folder can still hit git's own limits.

### Changed
- The code now follows the Google Python Style Guide, and CI enforces the parts a tool can check: every public module, class, function and method has a
  docstring, every function has type annotations (`mypy` now requires them), exceptions end in `Error`, and a broad `except Exception` needs a stated
  reason. No behaviour changed. CONTRIBUTING.md has a "Code style" section that also lists where the project deliberately differs from the guide.

## [0.4.0] - 2026-10-09

### Added
- **Who wrote it.** For a changed note that looks suspicious, or that changed outside git, `memdebug check` and `memdebug watch` now also say which
  logged Claude Code session wrote it, from that agent's session logs ("who wrote it: ..."), or that no logged edit explains it. It returns only a
  session id, a time and a tool name, never a path or any text from a log. It is evidence, not proof: a deleted log looks the same as a note nobody
  wrote, and shell-made changes cannot be matched (they are only counted). See `docs/threat-model.md`.
- The viewer has an **Agents** page (`memdebug serve`): the known agents that appear to be installed and what memdebug could watch for each, with the command
  to run (`memdebug setup`). It only checks that folders exist, opens no file, and works even while the ledger is busy.
- Cline is in the agent catalog: `memdebug agents` and `memdebug setup` offer its global rules folder (`~/Documents/Cline/Rules`) once it holds markdown rules.

### Changed
- `memdebug selftest`, "rollback is safe" and "hostile repository config cannot run programs": the control (ordinary git, tripwires armed) now runs in
  a separate repository with its own marker files, so nothing it leaves behind can be mistaken for an escape from the protected run, which is still
  asserted to run no tripwire. The tripwires now record what started them (time, arguments, and the parent and grandparent command lines where the
  platform shows them: `/proc` on Linux, `ps` on macOS, nothing on Windows), and a failure prints those records and `git --version`. This follows a
  failure on CI that passed on a re-run and could not be reproduced.
- The repository's ignore file also excludes `backups/` and `*.jsonl` (session logs), and the release hygiene test checks both rules.

### Documentation
- The README has badges, a recorded demo (`docs/demo.gif`, with the unpaced recording in `docs/demo.cast`), and new "Why this matters", "How it works" and
  "Engineering" sections, checked against the code; claims about snapshots, the ledger and rollback that overstated what the code does were corrected.
- CONTRIBUTING.md now starts with the private-data rule and covers reporting a security problem, setup, writing tests, changing the viewer and the pull
  request process; its second ground rule names both rollback engines. There is a pull request template and a bug-report form that tells people never
  to paste memory contents or session transcripts.
- `docs/threat-model.md` has a section on the session-log reader, and `docs/agents.md` has a row for Cline and records Claude Code's per-project memory
  folder as confirmed on Windows 11.

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
