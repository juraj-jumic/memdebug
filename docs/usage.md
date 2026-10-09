# Using memdebug

Everything beyond the first steps in the [README](../README.md): registering stores, the other commands, the viewer.

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

Which agents does it know? Claude Code, OpenClaw, Gemini CLI, Codex CLI, Windsurf, Cline and Open WebUI (in Docker): see [docs/agents.md](agents.md)
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

## Cleaning up old backups

A rollback of a plain folder saves what it replaced in `backups` next to the ledger, and these copies pile up. They hold your memory text, so
remove them when you no longer need them:

    memdebug backups list                                  # every backup, newest first, with its size and age
    memdebug backups clean --older-than 30                 # a dry run: shows what would be removed, removes nothing
    memdebug backups clean --keep 3 --older-than 30 --apply   # does it, after you confirm (add --yes to skip the question)

You must say which backups: `--older-than DAYS`, `--keep N` (the newest N of each store are never chosen), or `--all`; `--store NAME` limits it
to one store. A backup is chosen only if it meets every condition you give. Only folders shaped exactly like a rollback's are ever listed or
removed, anything else in the folder is reported and left alone, and a backup that holds a link is never removed. The age is read from the
backup's name. A removed backup cannot be brought back. Git notes keep their backups inside the repository, under `refs/memdebug/backups/`; these
commands do not touch those.

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

Security of the viewer, threat by threat: [docs/threat-model.md](threat-model.md#the-viewer). Its limits: it is plain HTTP on your own
machine, the first link (with the secret) stays in your browser history, and processes running as you can read the secret.
Choose Auto, Light or Dark at the top right.

## Fonts

The viewer uses only fonts already on your computer: Open Sans or Noto Sans if you have them, otherwise Segoe UI
(Windows) or the system font. Memory text, which is usually markdown, is shown in the monospace font a code editor
would use (Cascadia Mono or Consolas on Windows). Nothing is downloaded or bundled.
