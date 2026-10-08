# memdebug

[![CI](https://github.com/juraj-jumic/memdebug/actions/workflows/ci.yml/badge.svg)](https://github.com/juraj-jumic/memdebug/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/memdebug)](https://pypi.org/project/memdebug/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/memdebug/)
[![License](https://img.shields.io/github/license/juraj-jumic/memdebug)](LICENSE)

**See what your AI agent's memory holds, what changed, and put it back.** A local, agent-neutral tool for people who run
agents whose memory they can reach: notes in a folder or git repository, Open WebUI's memory, or a self-hosted Mem0.

![memdebug demo: an edit that bypassed git is caught and undone without losing anything, and a tampered copy of the record is detected](docs/demo.gif)

That is `memdebug demo` as released in 0.3.0, paced for reading (the real run takes about a second on Linux and a few seconds on Windows).
The unpaced recording is [`docs/demo.cast`](docs/demo.cast): `asciinema play docs/demo.cast`.

An agent's memory is built from text it read at runtime, and text can be planted: an email, a web page, a document.
Nobody reviews all of it. memdebug records what the memory holds in a tamper-evident ledger, shows what changed and when,
catches edits that bypassed the store's own history, compares snapshots, flags wording worth a second look, and rolls markdown memory
back safely.

It is an **observer**: it never sits between the agent and its memory, never talks to the agent, and runs entirely on your
computer. It does not block attacks as they happen (run it next to runtime guards). For Claude Code it can point at the logged session
behind a flagged change, as evidence and never as proof; for any other agent it cannot say *which conversation* wrote a memory. See
[docs/threat-model.md](docs/threat-model.md) for exactly what it does and does not do.

> **Status: alpha (0.3).** The parts described here work. The tests run on every push on Linux, Windows and macOS (Python 3.10, 3.12 and 3.14), and
> the author also runs them on Windows 11, but expect rough edges. [ROADMAP.md](ROADMAP.md) lists what is built and what is next.

## Why this matters

An agent's memory is text that is read back into the model at the start of every session. That makes it an unusual kind of state: it
persists, nobody reviews it, and anything the agent reads (an email, a web page, a document) can try to write itself into it. A poisoned
note does not fail loudly. It quietly changes behaviour later, in sessions that look unrelated. memdebug treats memory like any other
critical state: something to inspect, compare, prove and recover. The design problems it takes on:

* **Non-deterministic behaviour, deterministic state.** You cannot replay a language model, but you can pin down what it was *given*. A
  snapshot is the memory's text at a moment, as memdebug read it (line endings normalised, and a very long text cut with a hash of the whole),
  a diff is exactly what changed between two snapshots, and for git notes and plain folders a rollback puts a known-good state back. "What was
  the agent working from when it did that?" becomes a question you can answer from evidence, provided memdebug was watching at the time.
* **Views of memory that can disagree.** For markdown in git and for Mem0 there are three: the store's own history, what is actually there
  now, and memdebug's own record. They are reconciled, and the case that matters most is flagged: a change that **bypassed the store's own
  history**. A plain folder and Open WebUI keep no history, so there memdebug records what changed between two looks but cannot call
  anything "outside the history".
* **A record that shows tampering.** Entries are only ever appended, and each is hash-chained to the one before, so editing or removing an
  old entry is detectable (`memdebug verify`). Someone who can rewrite the whole ledger file can rebuild a valid chain, and cutting off the
  newest entries is only detectable against a copy of the head hash kept somewhere else (`memdebug witness`). The threat model says so.
* **Safe recovery, not just detection.** A rollback is a plan you review first, and the plan writes nothing. What it would replace is saved
  before anything is touched: for a plain folder every replaced file is copied byte for byte into a private backup folder next to the ledger;
  for git notes, anything git does not already hold is saved under `refs/memdebug/backups/`. The written files are checked against the plan,
  a failure while writing puts the files back, and the rollback is recorded in the ledger and can itself be undone.
* **Hostile input by design.** Memory text is untrusted: its size is bounded, it is never executed, and everything shown is escaped (terminal
  output through `safe_text`, the viewer through an escaping HTML builder with no JavaScript). git runs with a scrubbed environment and with
  hooks, filters, pagers, external diff helpers and file-system monitors switched off. `memdebug selftest` checks the platform-dependent
  protections on your own machine instead of asking you to take them on trust, and [docs/threat-model.md](docs/threat-model.md) lists what is
  protected and what is not.

What it deliberately is not: it does not hook into your agent, intercept prompts or optimise tokens. It reads the stores your agent already
writes, from outside (and, for Claude Code, its session logs, read-only, only to say which session logged an edit to a flagged note).

## Try it in a minute

You need Python 3.10 or newer and git 2.31 or newer. Install memdebug from PyPI into its own virtual environment, which works the same way
everywhere:

    python -m venv memdebug-env
    memdebug-env\Scripts\activate          # Windows (PowerShell or cmd); on macOS and Linux: source memdebug-env/bin/activate
    pip install memdebug

If you use [pipx](https://pipx.pypa.io/), `pipx install memdebug` does the same and keeps the `memdebug` command available everywhere. pipx is not
installed on Windows by default; to get it: `py -m pip install --user pipx`, then `py -m pipx ensurepath`, then open a new terminal. If your shell
cannot find the `memdebug` command after installing, `python -m memdebug` (on Windows `py -m memdebug`) does the same thing.

Then:

    memdebug demo                 # made-up agent, made-up attack, the real tools; nothing of yours is touched
    memdebug demo --serve         # ...and then look at it in the browser viewer

(To work on memdebug itself, install from a checkout instead: see [CONTRIBUTING.md](CONTRIBUTING.md).)

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
| folder | markdown notes in a plain folder (for example Claude Code's per-project memory folder) | none: changes are noticed between looks | not applicable | yes, from a snapshot (see below) |
| openwebui | the `memory` table of Open WebUI's `webui.db`, copied from its Docker container (or from a copy you made) | none | not applicable | no |
| mem0 | self-hosted Mem0 | Mem0's `history.db` | a change made directly in storage | no |

Which agents does it know? Claude Code, OpenClaw, Gemini CLI, Codex CLI, Windsurf, Cline and Open WebUI (in Docker): see [docs/agents.md](docs/agents.md)
for where each keeps its memory and where that was verified. Where memory sits in one file beside credentials (`~/.gemini`, `~/.codex`,
`~/.claude`), memdebug watches only that file. If Open WebUI runs in Docker, `memdebug setup` finds it and takes a read-only copy of its database before every look; there is nothing to
set up by hand (`windows-tools/copy-webui-db.ps1` is only a fallback for other setups). A store with no history can still be watched, but memdebug cannot tell an
attacker's edit from your own: it records what changed and when. ChatGPT, Claude's apps, Gemini and Copilot keep memory in the
provider's cloud: there is nothing local to watch, and memdebug says so rather than claiming to have found them.

## Tools for people who want more

    memdebug snapshot store NAME [--label TEXT]        # save a known-good copy of a watched store, to roll back to later
    memdebug rollback store NAME --to s1               # a dry run; add --apply. Works for folders and git notes
    memdebug timeline | verify | diff s1 s2 --full | snapshot ... | rollback markdown --path REPO --to s1
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

`memdebug serve` starts a read-only web page (timeline with an inspector, snapshots, compare, the agents it
found, integrity) on **127.0.0.1 only**, and prints a link that contains a random secret. Open that link;
the secret moves into a cookie and disappears from the address bar. It uses only Python's standard
library and sends no JavaScript at all. Run `memdebug verify` once first if the ledger is from an
older version (the viewer itself never upgrades or creates anything).

The viewer can only look, so where you may want to act it shows you the command instead, as text to paste into a terminal: a snapshot page, the
compare page, the page of a change made outside a store's history, and the overview's "Needs a look" all say how to put a watched store back
(`memdebug rollback store NAME --to sN`), and for an outside change they point at the last snapshot taken *before* it, never one that already includes
it. Stores memdebug only reads (Open WebUI, Mem0) say so instead of offering a command. A rollback of a plain folder is described as it really is:
changed in place, with the replaced files saved in a backup folder, not as a git commit.

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

## How it works

```
                    your agent (Claude Code, Open WebUI, Mem0, ...)
                    runs as usual: memdebug never hooks into it
                                       |  writes its memory
                                       v
        +--------------------------- memory store ---------------------------+
        |  markdown notes in git  |  plain folder  |  Open WebUI  |  Mem0     |
        +------------------------------------+------------------------------+
                                             |  read-only readers: nothing the store holds is executed
                                             v
   +-------------------+   +---------------------------------+   +--------------------------+
   | sync              |-->| events: added, changed, deleted,|-->| hints: "worth a second   |
   | the store's       |   | OUTSIDE the store's history     |   | look" (heuristics)       |
   | history vs what   |   +----------------+----------------+   +--------------------------+
   | is there now vs   |                    | append
   | the ledger        |                    v
   | (check, watch and |   +-------------------------------------+
   | snapshot run it)  |   | ledger: hash-chained, tamper-evident |   snapshots = the text
   +-------------------+   | + snapshots                          |   of the memory at a moment
                           +-----+----------------+---------+-----+
                                 |                |         |
                                 v                v         v
                       check / watch /       serve (read-only   diff s1 s2
                       timeline / verify     browser viewer)    compare two moments
                       in the terminal
                                 |
                                 |  you decide to go back
                                 v
   rollback:  plan (writes nothing) -> you confirm -> save what is replaced
              -> write -> check the written files against the plan -> record in the ledger
              a failure while writing puts the files back (if only the final ledger entry fails,
              the files stay restored and you are told)
              the only code that writes to a memory store: git notes and plain folders, never databases
```

memdebug's own files (the ledger, the list of watched stores, folder-rollback backups, copies of Open WebUI's database) live in its own data
folder, or where you point `--db`, `--out` or `--file`; memdebug does not write them inside a store. For Claude Code, `check` and `watch` also read
its session logs (read-only, ids and times only) to say which session logged an edit to a flagged note: evidence, not proof.

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

## Engineering

* **Tests:** 1,500+ `pytest` tests, run on every push to `main` and every pull request on Linux, Windows and macOS with Python 3.10, 3.12 and
  3.14, including platform-specific cases (real NTFS junctions made with `mklink /J`, case-insensitive file names, CRLF). CI also runs
  `memdebug selftest` and `memdebug demo` on each of those, and builds the package and installs it into a clean environment.
* **Protections are proven, not assumed:** the project's rule is that a protection only counts once removing it makes a test fail. See
  [CONTRIBUTING.md](CONTRIBUTING.md).
* **A self-test on your machine:** `memdebug selftest` demonstrates the platform-dependent safety claims on the computer you actually use,
  and says SKIP, never PASS, for anything it could not prove there.
* **Releases:** PyPI trusted publishing (no stored token), a manual approval before anything is published, provenance attestations (PyPI
  holds them for 0.3.0), and branch and tag rules on `main` and on version tags.
* **Written down:** a [threat model](docs/threat-model.md), a [security policy](SECURITY.md), a [changelog](CHANGELOG.md) and a
  [roadmap](ROADMAP.md) that says what is not built yet.

## Rolling back a watched store (folders and git notes)

`memdebug rollback store NAME --to s1` does the same job by the name you see in `memdebug stores`, for a plain folder of notes as well as for
git notes. First save a known-good copy while the store is healthy:

```
memdebug snapshot store claude-code-shop --label "good, after the move"     # a snapshot you can come back to
memdebug snapshot list                                                       # names and dates of snapshots
memdebug rollback store claude-code-shop --to s2                             # shows what would change; writes nothing
memdebug rollback store claude-code-shop --to s2 --apply                     # does it, after you type the snapshot id
```

`snapshot store` refuses to save while the ledger holds a change made outside the store's own history, or wording flagged as worth a second look,
since the store's last snapshot: a snapshot is what a rollback later treats as good. Look at the flagged changes first (`memdebug serve`), then add
`--include-changes` if they are fine. This does not go away if you run it twice.

For a **plain folder** there is no git to take exact file versions from, so it works from the snapshot's text, and it is honest about what that means:

* **Backups are files.** Anything the rollback would replace or remove is first copied, byte for byte, into a private folder next to your ledger
  (`backups\<store>\<date>-<id>`, with a `manifest.json`), and each copy is read back and checked before your notes are touched. Get a file
  back by copying it out of that folder's `files` folder. These copies contain your memory text and memdebug never deletes them: delete old ones
  yourself when you no longer need them.
* **Line endings.** A snapshot does not keep them. Files come back as UTF-8 with LF line endings, or with CRLF if the file being replaced uses CRLF
  throughout. A file with mixed endings comes back with LF.
* **Nothing it cannot restore faithfully is written.** Text that was cut, marked too large, or had bytes that are not valid text is skipped, and
  the dry run says which files and why.
* **Everything else is the same** as for git notes: the plan writes nothing, applying refuses if anything changed since you saw the plan, links,
  junctions and unsafe names are refused, every write is undone if a later step fails, and the rollback is recorded and can itself be undone.

Open WebUI and Mem0 keep their memory in databases that memdebug only ever reads, so they cannot be rolled back; change those in the app.

## Rolling back markdown in git, by path

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

`memdebug selftest` proves the "no programs run" and "exact bytes" claims, and the folder-rollback claims, on your computer.

## Fonts

The viewer uses only fonts already on your computer: Open Sans or Noto Sans if you have them, otherwise Segoe UI
(Windows) or the system font. Memory text, which is usually markdown, is shown in the monospace font a code editor
would use (Cascadia Mono or Consolas on Windows). Nothing is downloaded or bundled.

## License

Copyright 2026 Juraj Jumić. Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
