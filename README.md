# memdebug

**See what your AI agent's memory holds, what changed, and put it back.** A local, agent-neutral tool for people who run
agents whose memory they can reach: notes in a folder or git repository, Open WebUI's memory, or a self-hosted Mem0.

An agent's memory is built from text it read at runtime, and text can be planted: an email, a web page, a document.
Nobody reviews all of it. memdebug records what the memory holds in a tamper-evident ledger, shows what changed and when,
catches edits that bypassed the store's own history, compares snapshots, flags wording worth a second look, and rolls markdown memory
back safely.

It is an **observer**: it never sits between the agent and its memory, never talks to the agent, and runs entirely on your
computer. It does not block attacks as they happen (run it next to runtime guards), and it does not yet say *which
conversation* wrote a memory. See [docs/threat-model.md](docs/threat-model.md) for exactly what it does and does not do.

> **Status: alpha (0.2).** The parts described here work. The tests run on every push on Linux, Windows and macOS (Python 3.10, 3.12 and 3.14), and
> the author also runs them on Windows 11, but expect rough edges. [ROADMAP.md](ROADMAP.md) lists what is built and what is next.

## Try it in a minute

    pip install .                 # needs Python 3.10+ and git 2.31+
    memdebug demo                 # made-up agent, made-up attack, the real tools; nothing of yours is touched
    memdebug demo --serve         # ...and then look at it in the browser viewer

The demo plants an instruction into a note behind git's back, shows memdebug catching it, rolls the file back without losing
the planted text, and shows the ledger noticing a tampered copy. It works in a throwaway folder and removes it afterwards.

## Watch your own agent's memory

You point memdebug at the memory; it does not hook into the agent. The easy way is the guided setup:

    memdebug agents      # which AI agents are on this computer, and what each keeps (looks at folder names only)
    memdebug setup       # finds that memory, asks before adding anything, saves a first snapshot of each
    memdebug check       # looks for changes once; exit code 1 means something needs a look
    memdebug watch       # keeps looking and says so when something changes (Ctrl+C to stop)
    memdebug serve       # the same story in your browser, read-only

Or register stores yourself (this is what a script would do):

    memdebug add ~/agent/notes                    # a folder of markdown notes
    memdebug add ~/agent/memory-repo              # markdown notes in a git repository (also reads its history)
    memdebug add --docker open-webui              # Open WebUI running in Docker (memdebug copies its database itself)
    memdebug add ~/copies/webui-copy.db           # Open WebUI's memory, from a copy of webui.db you made yourself
    memdebug add ~/.mem0/history.db --user-id me  # self-hosted Mem0 (needs: pip install "mem0ai>=2")
    memdebug stores | status | remove NAME

| Store | What it is | Change history | "Changed outside the history" | Rollback |
| --- | --- | --- | --- | --- |
| markdown (git) | markdown notes in a git repository | git history | an uncommitted edit | yes |
| folder | markdown notes in a plain folder (for example Claude Code's per-project memory folder) | none: changes are noticed between looks | not applicable | not yet |
| openwebui | the `memory` table of Open WebUI's `webui.db`, copied from its Docker container (or from a copy you made) | none | not applicable | no |
| mem0 | self-hosted Mem0 | Mem0's `history.db` | a change made directly in storage | no |

Which agents does it know? Claude Code, OpenClaw, Gemini CLI, Codex CLI, Windsurf and Open WebUI (in Docker): see [docs/agents.md](docs/agents.md)
for where each keeps its memory and where that was verified. Where memory sits in one file beside credentials (`~/.gemini`, `~/.codex`,
`~/.claude`), memdebug watches only that file. If Open WebUI runs in Docker, `memdebug setup` finds it and takes a read-only copy of its database before every look; there is nothing to
set up by hand (`windows-tools/copy-webui-db.ps1` is only a fallback for other setups). A store with no history can still be watched, but memdebug cannot tell an
attacker's edit from your own: it records what changed and when. ChatGPT, Claude's apps, Gemini and Copilot keep memory in the
provider's cloud: there is nothing local to watch, and memdebug says so rather than claiming to have found them.

## Tools for people who want more

    memdebug timeline | verify | diff s1 s2 --full | snapshot ... | rollback markdown --path REPO --to s1   # a dry run; add --apply
    memdebug report --format markdown|json|sarif [--out FILE] [--fail-on findings|hints]    # for people, programs and CI
    memdebug witness --file E:\memdebug-witness.txt      # a second copy of the ledger's fingerprint, kept somewhere else
    memdebug verify --witness E:\memdebug-witness.txt    # catches a rewritten or cut-short ledger
    memdebug check --strict                                # also exit 1 when changed wording looks worth a second look

Where a store records it, memdebug also notes where a memory came from. For Open WebUI that is the app's own label (for example `created_by: manual`)
and whether a chat was active at the time ("consistent with being added by hand" or "a chat was active"). It is evidence, never proof, and it never
makes anything "trusted".

Hints ("worth a second look") are guesses from the wording of what was added: instructions to send something to an address, to
stop asking for confirmation, to weaken a safeguard, hidden characters, secret-looking strings. They miss things (paraphrases,
many languages) and can flag harmless text, so they never count as a verdict. A secret-looking string is never repeated in a hint.

Without `--db` the ledger and the list of stores go to a per-user folder (`%LOCALAPPDATA%\memdebug` on Windows,
`~/.local/share/memdebug` elsewhere), never the current folder, which could be inside the repository being watched. Use
`python -m memdebug ...` if the `memdebug` command is not on PATH. Treat the ledger as sensitive: it contains your agent's memory
text. Then run `memdebug selftest` once on any new machine: it proves the platform-dependent protections hold there, and says SKIP
(never PASS) for anything it could not prove.

Keeping `watch` running: on Windows, create a shortcut to `memdebug watch` in the Startup folder; on Linux or macOS use a systemd
user service or a login item. memdebug does not install anything that starts by itself.

## Documentation

* [docs/threat-model.md](docs/threat-model.md): what is defended, against whom, and the known limits.
* [ROADMAP.md](ROADMAP.md): built, next, and not planned. [CHANGELOG.md](CHANGELOG.md): what changed. [docs/releasing.md](docs/releasing.md): how releases are made.
* [SECURITY.md](SECURITY.md): how to report a problem. [CONTRIBUTING.md](CONTRIBUTING.md): how to help.

## The viewer

`memdebug serve` starts a read-only web page (timeline with an inspector, snapshots, compare,
integrity) on **127.0.0.1 only**, and prints a link that contains a random secret. Open that link;
the secret moves into a cookie and disappears from the address bar. It uses only Python's standard
library and sends no JavaScript at all. Run `memdebug verify` once first if the ledger is from an
older version (the viewer itself never upgrades or creates anything).

Security of the viewer, threat by threat: [docs/threat-model.md](docs/threat-model.md#the-viewer). Its limits: it is plain HTTP on your own
machine, the first link (with the secret) stays in your browser history, and processes running as you can read the secret.
Choose Auto, Light or Dark at the top right.

## Windows

Written for Windows as well, with Windows-specific code paths. The test suite and `memdebug selftest` have been run on Windows 11, and the CI workflow covers `windows-latest`, but run `memdebug selftest` on your own machine anyway; it says SKIP, with a reason, for anything
it cannot prove (for example symlinks need Developer Mode).

- Needs Git for Windows 2.31+ (a real `git.exe`; shims and scripts are refused). `core.autocrlf=true` (the installer default)
  is handled: line endings are normalised before comparing.
- git is found through PATH entries that are absolute paths only; the current folder is never searched.
- Names that mean something special on Windows (`NUL.md`, `con.md`, `file:stream.md`, trailing dots or spaces, `GIT~1`, `.GIT`)
  are rejected on every platform, so the history and the files always agree.
- Directory junctions and symlinks are never followed, including by rollback. A hung git is stopped with `taskkill /T`.
- Ledger file permissions are not enforced by this tool on Windows; the default location is private to your user account.
- If git reports "dubious ownership" for a repository on another drive, fix the ownership; this tool deliberately ignores your
  global `safe.directory` setting.

## How it is built

The core (model, ledger, sync, reconcile) imports no backend. Each store type is a read-only adapter: `markdown-git` and `folder`
(markdown files), `openwebui` and `mem0` (databases). Mem0 is imported lazily in one function; the whole suite passes with Mem0 not
installed. Adding a store type is described in [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

    pip install -e ".[dev]"
    pytest -n auto
    memdebug selftest
    ruff check src tests && mypy

See [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules (everything read from a store is untrusted; adapters only read).

## Rolling back (markdown/git)

`memdebug rollback markdown` puts a memory folder back to what a snapshot held. It is the only command that changes
your files, so it is cautious by design.

```
memdebug rollback markdown --path C:\path\to\memory --to s1            # shows what would change; writes nothing
memdebug rollback markdown --path C:\path\to\memory --to s1 --apply    # does it, after you type the snapshot id
```

Options: `--only FILE` (repeatable) restores just those files; `--remove-added` also removes files added since the
snapshot (by default they are left alone); `--full` shows the line changes; `--yes` skips the typed confirmation.

What it guarantees:

* **Nothing is rewritten.** Files that must change in git become one new commit, written by `memdebug`, on top of
  your branch. Your history stays exactly as it was.
* **Nothing is lost.** Anything git does not already hold that the rollback would replace or delete (an uncommitted
  edit, a file that was never committed) is saved first under `refs/memdebug/backups/...`. Get a file back with
  `git checkout <that name> -- <file>`.
* **Exact bytes.** Files come back byte for byte from the matching version in git history, line endings included. A
  snapshot's normalised text is used only when git never held that version, and never if it was cut, too large or
  not valid text.
* **No programs run.** Hooks, filters, fsmonitor and other helpers named in the repository are never executed.
* **It refuses unsafe states:** a detached HEAD, staged changes, a merge or rebase in progress, a git lock, links,
  junctions, device names, or paths that differ only by letter case.
* **It undoes itself** if any step fails, and says so.
* **It can be undone.** It takes a snapshot just before and just after, and records a `ROLLBACK` entry in the ledger.
  To undo a rollback, roll back to the "before" snapshot it printed.

`memdebug selftest` proves the "no programs run" and "exact bytes" claims on your computer.

## Fonts

The viewer uses only fonts already on your computer: Open Sans or Noto Sans if you have them, otherwise Segoe UI
(Windows) or the system font. Memory text, which is usually markdown, is shown in the monospace font a code editor
would use (Cascadia Mono or Consolas on Windows). Nothing is downloaded or bundled.

## License

Copyright 2026 Juraj Jumić. Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
