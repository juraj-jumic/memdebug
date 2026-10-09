# How memdebug works

The ideas behind memdebug, how the pieces fit together and how the project is tested. The short version is in the [README](../README.md).

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
  protections on your own machine instead of asking you to take them on trust, and [docs/threat-model.md](threat-model.md) lists what is
  protected and what is not.

What it deliberately is not: it does not hook into your agent, intercept prompts or optimise tokens. It reads the stores your agent already
writes, from outside (and, for Claude Code, its session logs, read-only, only to say which session logged an edit to a flagged note).

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
installed. Adding a store type is described in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Engineering

* **Tests:** 1,500+ `pytest` tests, run on every push to `main` and every pull request on Linux, Windows and macOS with Python 3.10, 3.12 and
  3.14, including platform-specific cases (real NTFS junctions made with `mklink /J`, case-insensitive file names, CRLF). CI also runs
  `memdebug selftest` and `memdebug demo` on each of those, and builds the package and installs it into a clean environment.
* **Protections are proven, not assumed:** the project's rule is that a protection only counts once removing it makes a test fail. See
  [CONTRIBUTING.md](../CONTRIBUTING.md).
* **A self-test on your machine:** `memdebug selftest` demonstrates the platform-dependent safety claims on the computer you actually use,
  and says SKIP, never PASS, for anything it could not prove there.
* **Releases:** PyPI trusted publishing (no stored token), a manual approval before anything is published, provenance attestations (PyPI
  holds them for every release since 0.3.0), and branch and tag rules on `main` and on version tags.
* **Written down:** a [threat model](threat-model.md), a [security policy](../SECURITY.md), a [changelog](../CHANGELOG.md) and a
  [roadmap](../ROADMAP.md) that says what is not built yet.
