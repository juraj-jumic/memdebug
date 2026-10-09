# Roadmap

Where memdebug is and where it is going. It is alpha software: the pieces below that are built work and are tested,
but expect rough edges and breaking changes before 1.0. Order is a plan, not a promise.

## Built (0.6)

* Tamper-evident ledger (hash chain) with `verify`, snapshots whose content is chained in, and compare.
* **Witness:** a second copy of the ledger's head kept elsewhere, so a rewritten or cut-short ledger is noticed.
* Stores: markdown in a git repository, plain folders of markdown, Open WebUI's memory (copied from its Docker container, or from a copy you
  made),
  and self-hosted Mem0 (2.2.1). All read-only. Stores without a history record what is seen between looks.
* **An agent catalog:** `memdebug agents` and `setup` recognise Claude Code, OpenClaw, Gemini CLI, Codex CLI, Windsurf, Cline and Open WebUI in
  Docker, with locations taken from their documentation (see docs/agents.md).
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
* A read-only browser viewer: timeline, redlined changes, snapshots, compare, the agents it found, integrity, light and dark.
* **Windows paths over 259 characters** are read, checked, rolled back and backed up without changing any Windows setting.
* **Rollback for markdown/git:** dry run first, exact bytes, backups of anything git does not hold, no rewritten
  history, undo on failure, recorded in the ledger, and itself undoable.
* `memdebug selftest` (proves the platform-dependent protections on your machine) and `memdebug demo`.
* Lint, type checking and CI on Linux, Windows and macOS (Python 3.10, 3.12 and 3.14), plus a release workflow with a no-publish dry run.

## Next, roughly in this order

1. **Rollback for plain folders: built** (see Built). Still to do: trying it on real agent memory folders.
2. **More provenance.** First slices exist for Open WebUI (the app's own label and how close a chat was) and for Claude Code (which logged Write or Edit
   call wrote a flagged note). Still to do: which conversation turn wrote a memory and what the assistant had just read, showing it in the viewer
   (which would have to read `stores.json`), and readers for other agents' session logs (OpenClaw).
   The earlier plan, in full: **Provenance from session logs.** Which conversation turn wrote a memory, and whether it came from the user or from
   something the agent read. This turns a change log into an incident-response tool. It needs a reader per agent (first
   candidates: Open WebUI chat records, Claude Code session transcripts), and depends on seeing real log formats.
3. **More agents,** each added only once its memory location is documented. Cline is in. Left: Goose (its memory is `.txt` files, which the folder
   reader does not read, and its Windows location is unconfirmed) and Continue (its global rules folder is not in its official documentation).
   Cursor and Aider keep nothing under the home folder to watch, and Claude Desktop's memory is in the cloud.
4. **Releases.** 0.2.0 was published to GitHub and PyPI on 2026-10-07 through the tag-triggered workflow (PyPI trusted publishing, with a
   required approval and provenance attestations). The install from PyPI has been checked in a clean Linux environment and on Windows 11 (pipx, Python 3.14); macOS
   still needs the same check. Still to do: signed release notes and a smoother route for people without Python (item 5).
5. **Better setup for non-advanced users:** stand-alone programs (no Python) are built for Windows, Linux and macOS and attached to each release (not signed). Still to do:
   a signed Windows installer, and a way to keep `watch` running without a terminal.
6. **Viewer:** filter by store, show hints in the overview, a status page that matches `memdebug status`.

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
