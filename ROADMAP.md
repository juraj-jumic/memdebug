# Roadmap

Where memdebug is and where it is going. The pieces below that are built work and are tested; the interfaces in docs/stability.md are stable from 1.0.
Order is a plan, not a promise.

## Built (1.0)

* Tamper-evident ledger (hash chain) with `verify`, snapshots whose content is chained in, and compare.
* **Witness:** a second copy of the ledger's head kept elsewhere, so a rewritten or cut-short ledger is noticed.
* Stores: markdown in a git repository, plain folders of markdown or text notes, Open WebUI's memory (copied from its Docker container, or from a copy you
  made), the official MCP memory server's `memory.jsonl` (each entity and relation one memory),
  and self-hosted Mem0 (2.2.1). All read-only. Stores without a history record what is seen between looks.
* **An agent catalog:** `memdebug agents` and `setup` recognise Claude Code, OpenClaw, Gemini CLI, Codex CLI, Windsurf, Cline, Roo Code, Hermes Agent, Goose,
  GitHub Copilot CLI, Zed, Basic Memory and Open WebUI in Docker, with locations taken from their documentation or source and a plain statement of which were tried
  on a real installation (see docs/agents.md), and the variables that move an agent's folder honoured.
* **A registry of stores and guided use:** `setup` (finds likely memory, asks first), `add`, `stores`, `remove`, `check`
  (exit code 1 when something needs a look or a note could not be read), `status`, `watch`.
* Detection of changes that bypassed a store's own history, with false-alarm guards.
* **Reports** in Markdown, JSON and SARIF, with CI exit codes.
* **Rollback for plain folders** (`memdebug rollback store`, and `memdebug snapshot store` to save a known-good copy first), rebuilt from the snapshot's
  text with backups of anything replaced, which `memdebug backups list` and `clean` show and tidy; Open WebUI and Mem0 still cannot be rolled back.
* **Published on PyPI** as `memdebug` (`pip install memdebug`), released by a tag-triggered workflow that needs an approval click.
* **Stand-alone programs** for Windows, Linux and macOS (no Python needed), attached to each GitHub release with checksums and a build attestation; not signed.
* **A first slice of provenance for Open WebUI:** its own `created_by` label and a chat-timing comparison, as evidence only.
* **Who wrote it, for Claude Code:** `check` and `watch` name the logged Claude Code session whose Write or Edit call changed a flagged note, from its
  session logs, as evidence and never proof. Changes made by shell commands cannot be matched, so they are only counted.
* **Hints** ("worth a second look"): heuristics for instructions to send data, remove confirmation or weaken safeguards,
  hidden characters and secret-like strings. Labelled as guesses; secrets are never repeated.
* A read-only browser viewer: timeline, redlined changes, snapshots, compare, the agents it found, **the status of the latest check (including a warning when a
  store was not read in full)**, integrity, light and dark.
* **A background watcher:** `memdebug autostart` sets up `watch` to run from login (a scheduled task, a systemd user service or a launch agent), shown first and
  applied only with `--apply`; `watch --log` keeps a small log, and the watcher reads its store list again before every look.
* **Stability:** docs/stability.md says what is stable; ledgers written by real releases 0.2.0 to 0.6.1 are tested against the current code, the hash format and
  every promised format are pinned by tests, and the JSON report carries a schema version.
* **Windows paths over 259 characters** are read, checked, rolled back and backed up without changing any Windows setting.
* **Rollback for markdown/git:** dry run first, exact bytes, backups of anything git does not hold, no rewritten
  history, undo on failure, recorded in the ledger, and itself undoable.
* `memdebug selftest` (proves the platform-dependent protections on your machine) and `memdebug demo`.
* Lint, type checking and CI on Linux, Windows and macOS (Python 3.10, 3.12 and 3.14), plus a release workflow with a no-publish dry run.

## Next, roughly in this order

memdebug 1.0 means the interfaces in [docs/stability.md](docs/stability.md) are stable, not that everything is finished. What is still open:

1. **Reading only what is new in a long git history.** A git store beyond 100,000 file changes or 100,000,000 characters of edits is only partly read, and
   `check` says so loudly ("NOT READ IN FULL") instead of calling it quiet; reading just the commits since the last look is not built.
2. **Trying things on real installations.** Only Claude Code (Windows 11) and Open WebUI (Docker) have been tried by a person. The other catalog entries follow documentation
   or source code (see docs/agents.md); the background watcher's Linux and macOS forms are tested only as plans and commands (the Windows one was tried once on a real Windows 11 computer, not through a restart);
   the Linux and macOS programs are tested by CI only. Reports from people who use these are the most useful thing anyone can give the project.
3. **More provenance.** Which conversation turn wrote a memory and what the assistant had just read; Open WebUI's label cannot tell the assistant's own writes from a person's
   (newer versions record the model and chat for the `update_memory` tool, which is shown). Session-log readers for agents other than Claude Code (OpenClaw).
4. **More agents,** each added only once its memory location is documented. Not yet: Continue and Cursor (nothing under the home folder), the VS Code Copilot memory tool and the
   Antigravity CLI (paths unverified), Zed's Rules Library and LibreChat (databases).
5. **Releases.** A signed Windows program (not now: see docs/windows.md for why unsigned programs warn), and a check of the install on macOS.
6. **Viewer:** filter by store and hints in the overview.

## Later, maybe

* Mem0 rollback (no undo exists in Mem0; this would re-add, update and delete through its API, with weaker guarantees).
* Signed entries with a key kept off the host; encryption at rest.
* More hints (write bursts across a whole store), and hints in more languages.

## Not planned

* Blocking or rewriting the agent's memory calls at runtime. memdebug is an observer on purpose: it needs no
  integration with the agent, and cannot break it.
* A hosted service, accounts, or a multi-user server. It stays local-first.
* Replaying or undoing what an agent did because of a bad memory.
* Reading chats, passwords or settings of any application. Adapters read memory only.

Have a use that needs something sooner? Open an issue.
