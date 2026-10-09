# Threat model

What memdebug defends, against whom, where each defence lives, and what it does **not** do. If a claim here is not
backed by a test, that is a bug in this document: please report it.

## What memdebug is

A local, agent-neutral **observer** for an AI agent's memory. It reads a memory store (markdown files in a git
repository, or a self-hosted Mem0), records what it holds in a tamper-evident ledger, compares snapshots, shows them in a
read-only viewer, and can restore markdown files to a snapshot. It does **not** sit between the agent and its memory,
does not block anything while the agent runs, and never talks to the agent. Run it next to runtime guards, not instead
of them.

The threat it addresses is *memory poisoning*: text that an attacker gets an agent to store (through an email, a web
page, a document) and that steers the agent later. Public taxonomies name it OWASP's agentic-application risk ASI06
(memory and context poisoning) and MITRE ATLAS technique AML.T0080 (AI memory poisoning). The questions memdebug helps
answer afterwards: *what does the memory hold now, what changed and when, did anything change without going through the
store's own history, and can I put it back?*

## Assets

1. **Your memory files** (they steer your agent, and rollback writes to them).
2. **The ledger and snapshots**: a record of everything the memory held, including deleted memories. Sensitive.
3. **Your machine**: it runs git and parses data it did not create.
4. **The viewer's secret**, which gates read access to the ledger's contents.

## Adversaries in scope

| Adversary | Capability |
| --- | --- |
| Memory planter | Gets text into the store through content the agent reads. Cannot touch your machine otherwise. |
| Hostile repository or store | A repository or database you point the tool at, carrying hostile config, links, odd names, huge or malformed data. |
| Other local program or user | Can talk to local ports and read files your account can read. |
| A web page in your browser | Tries to reach the viewer on 127.0.0.1. |
| Someone editing the ledger file | Edits, deletes or truncates entries, snapshots or the tail. |

Out of scope: an attacker who already controls your account or machine (root, malware running as you), flaws in git,
Python or the operating system, side channels, and denial of service against the store itself.

## Reading and parsing untrusted data

| Threat | Defence |
| --- | --- |
| Terminal or log injection (escape codes, newlines, bidi or zero-width characters in memory text) | All output goes through `safe_text`: such characters are shown as visible escapes; one line per entry, always |
| Oversized text | Cut at 100,000 characters with a hash of the full text in the marker; files over 1 MiB are not read; total text per read is budgeted |
| Malformed or hostile rows (NULL, unknown events, BLOBs, invalid UTF-8, bad timestamps, huge ids) | Skipped or repaired per row with a capped warning; never a crash; skipped rows are counted |
| SQL injection | Fixed SQL; values are bound parameters |
| Reading adapters changing what they inspect | Mem0's database is opened `mode=ro` plus `query_only`; the markdown adapter only runs read-only git commands. Only `adapters/restore.py` writes (see Rollback) |
| Hostile file names (control characters, quotes, backslashes, `..`, `.git`, Windows device names like `NUL.md`, `file:stream`, trailing dots or spaces, `GIT~1`) | Rejected on both the history side and the working-tree side, on every platform, so the two cannot disagree |
| Links and special files pointing at secrets | `lstat` check plus `O_NOFOLLOW`; symlinks, FIFOs and devices skipped; Windows junctions and reparse points detected by attribute and never followed; the listing is marked incomplete |
| A note hidden from the monitor by Windows' path limit (a deep folder, or a long project folder name) | Windows refuses paths over 259 characters unless long paths are switched on, which is off by default, so such a note could be missed by every check and by a rollback. All note, backup and session-log access on Windows uses the extended-length form of the path, so it does not depend on that setting. Git itself has its own limits on a very deeply nested repository folder, which memdebug cannot change |
| Console that cannot show a character | Output is escaped to the console's encoding instead of raising |

## git and hostile repositories

| Threat | Defence |
| --- | --- |
| A repository's config, hooks, filters or attributes running programs | Scrubbed environment; no global or system config; fsmonitor, hooks, signature checking, attributes and quoting overridden on the command line; no external diff, no textconv, no pager; replace-refs disabled; no shell; fixed argument lists |
| A hung or runaway git | Wall-clock limit; the whole process group (Windows: `taskkill /T`) is killed; output, line and stderr caps |
| A `git` or `git.exe` planted in the folder you run the tool from | git is located only through absolute PATH entries; on Windows only a real `git.exe` is accepted |
| Reading file content through a path | History is read by object id, never by path |

`memdebug selftest` demonstrates the hostile-repository protections on your machine. For each attack it first shows, as
a control, that the attack works against ordinary git, and reports SKIP rather than PASS if it cannot.

## Ledger and snapshots

| Threat | Defence |
| --- | --- |
| An edited entry | Hash chain; `verify` checks links, hashes and index columns |
| An edited snapshot (text, flag, label, scope, time, entry list) | A snapshot's whole content is hashed and written into the chain; loading or verifying recomputes it |
| Silent snapshot deletion, or restoring a deleted one | Deletion is a chained event; `verify` reports missing or resurrected snapshots |
| Removed newest entries | Each snapshot records the entry it was taken after; `verify --expected-head` checks against a head hash you saved elsewhere |
| Bookkeeping forged as memory events | Snapshot, deletion and rollback entries can only be written by their own methods; they use their own index key; rollback records are re-validated on verify |
| False "changed outside the history" alarms | Same-scope memories only; never from a listing that hit a limit or had unreadable parts; never from a partly read history; a change must be seen in two passes. Changes written by a rollback are recognised and recorded as such |
| Concurrent syncs | One immediate transaction, unique backend row ids, retry on collision, all-or-nothing |

## The viewer

| Threat | Defence |
| --- | --- |
| Memory text containing HTML or script | Pages are built only through an escaping builder with allow-listed tags and attributes; there is no JavaScript; a Content-Security-Policy forbids scripts, inline styles, framing, images and external loads |
| A web page reaching the server (DNS rebinding) | `Host` must be exactly `127.0.0.1:PORT` or `localhost:PORT`; cross-site and foreign-origin requests are refused; the secret is required anyway |
| Other users or programs on the machine | 192-bit secret, HttpOnly SameSite=Strict cookie, constant-time comparison; nothing is served without it |
| Another program taking the port | The port is taken exclusively (matters on Windows); `selftest` checks it |
| Changing anything | Only GET and HEAD; the ledger is opened read-only. The theme switch sets one cookie and nothing else |
| A command shown for copying that a hostile ledger turned into something else | The "put it back" guidance is only text, never a link or a button. A store name from the ledger goes into a command only if it passes the same strict rule as a watched store's name (lowercase letters, digits, dot, dash, underscore); anything else gets a `<name>` placeholder, so pasting a command can never do more than the command says. Stores memdebug cannot write to never get one. For an outside change it names only a snapshot taken before it |
| Odd, oversized or slow requests | Strict limits on request line, path, query, headers, connections and time; only fixed routes and validated ids; nothing from the request is echoed |

## Who wrote a change (agent session logs)

`memdebug check` can say which logged Claude Code session wrote a flagged note, by searching that agent's session logs (`~/.claude/projects/*/*.jsonl`)
for edit calls on the note's path. The answer is **evidence, never proof**: anyone who can edit a memory folder can edit or delete a log, a deleted
log looks the same as a note nobody wrote, and a change made by a shell command has no path in the log, so it can only be counted. The logs are whole
conversations, so they are handled as hostile and private.

| Risk | Defence |
| --- | --- |
| Log text (instructions, escape codes, markup) reaching the screen or a page | Nothing from a log is returned except a session id and a record id (each must match a strict pattern ending in `\Z`), a time, and the name of one of a fixed list of tools. No path, no message text; the sentence shown is built from those values only and goes through `safe_text` |
| A hostile ledger name steering the search | A note's path is built only from an id that passes the same file-name check the store readers use; otherwise nothing is searched |
| Following a link or reading something that is not a session log | Only plain `.jsonl` files directly inside real project folders are opened, with `lstat` checks and `O_NOFOLLOW`; links, junctions and anything else are skipped |
| A huge, damaged or deeply nested log | Fixed limits on files, bytes per log, bytes in total and bytes per line; bad lines are skipped; when a limit stops the search the result says it is incomplete |
| Reading logs needlessly | Only changes that were noticed (not ones a store's own history explains) and that look suspicious or happened outside the history are searched, at most five per pass; a log not written since the period began is never opened |
| Over-trusting the answer | The wording says what it cannot show ("a deleted log looks the same, so this is not proof of anything"); a search that could not cover the whole period says so |

## Rollback (markdown/git)

Rollback is the only part of memdebug that changes your files, so it is the most constrained.

| Risk | Defence |
| --- | --- |
| Doing something you did not expect | Dry run by default; `--apply` needs the snapshot id typed (or `--yes`); the plan is recomputed at apply time and refused if anything changed since you saw it |
| Writing text that is not what the file held | Files come back as the exact original bytes from git history. Snapshot text, which is normalised, is a flagged fallback and is refused if it was cut, marked too large, or had undecodable bytes |
| Losing work | Anything git does not hold that would be overwritten or deleted (an uncommitted edit, an untracked file) is first saved as a commit under `refs/memdebug/backups/`, which survives garbage collection |
| Rewriting history | Changes that git needs become one new commit on top of your branch, built with git plumbing in a private index; the branch moves only if nobody committed in the meantime |
| A hostile repository running programs while we write | The same git hardening as reading; no `git add`, `commit` or `checkout` is used, so no hook, clean/smudge filter or attribute can run |
| Writing through a link or into the wrong place | Every path is validated; links, junctions and non-files are refused; folders are created one level at a time with a check; files are written to a temporary name and renamed; names that differ only by letter case are refused |
| Unsafe repository states | Refused: detached HEAD, staged changes, merge/rebase/cherry-pick in progress, git lock files |
| A failure half-way | Every step is journaled; on any failure (or Ctrl+C) files, index and branch are put back and you are told |
| An unrecorded rollback | The ledger record is validated before any file is touched; the state is snapshotted before and after; a `ROLLBACK` entry is chained into the ledger; undoing is rolling back to the "before" snapshot |


## Rollback of a plain folder

Rollback of a watched store by name (`memdebug rollback store`) uses the same file-writing code as the git engine (`adapters/fileops.py`), so links,
junctions, case clashes, atomic writes and the undo journal are identical. What differs, because a folder has no git history:

| Threat | Protection |
| --- | --- |
| Restoring text a snapshot could not keep faithfully | The snapshot's text is the only source. Text that was cut, marked too large or had undecodable bytes is refused and named in the plan, never written. Line endings are LF, or CRLF if the file being replaced uses CRLF throughout; mixed endings become LF. |
| Losing what the rollback replaces | Every file that would be overwritten or removed is first copied byte for byte into a private folder next to the ledger (0700/0600 on POSIX), the copy is read back and checked, and nothing in the notes is touched if that fails. |
| Backups landing inside the notes (and being read as memories), or the notes inside the backups | The plan refuses (a blocker) when either folder contains the other, and applying refuses again. |
| A "good" snapshot that is really an attack | `memdebug snapshot store` refuses while the ledger holds an outside-history change or flagged wording since the store's last snapshot. It reads the ledger, so looking again does not clear it; only a deliberate `--include-changes` does. |
| The folder changing between the plan and the write | Applying re-plans and refuses if the plan differs; every target is re-checked before the first backup or write. |
| A snapshot (or a tampered ledger) naming unsafe files | Names are validated by the same rule as the reader (no `..`, absolute, device or case-clashing names), and files outside the store's subfolder, or outside a named-files store, are never written. |

Known limits: backups hold your memory text and are deleted only when you run `memdebug backups clean --apply` (it removes only folders shaped
exactly like a rollback's, inside the backup folder, never follows a link, and leaves a backup that holds one alone); a plain-folder rollback cannot restore exact bytes for files whose
line endings were mixed; a store with no history cannot show an outside-history bypass, only the changes observed between looks. The Windows
permissions of the backup folder are those of your user profile (memdebug sets no ACL), and the junction guard is exercised on every platform by
simulation, but real junctions are only tested on Windows runs of the suite.

## Stores, settings and discovery

| Threat | Defence |
| --- | --- |
| A store with no history (plain folder, Open WebUI) | There is no "outside the history" to detect. Changes are recorded as observed adds, edits and removals, time-stamped when noticed. memdebug cannot tell an attacker's edit from yours there; it can show exactly what changed and when, and flag wording. The viewer's overview says so instead of claiming the changes "went through the store's own history" |
| The plain-folder reader following links, or reading odd names or huge files | It reuses the markdown adapter's reader: links and junctions are never followed, unsafe names and oversized files are skipped, the listing is marked incomplete. A git repository is refused (use the markdown type, which also reads its history) |
| Reading more of Open WebUI than memory (also when copied out of Docker) | Only the `memory` and `user` tables are queried (a test records every statement and checks that nothing else is touched); the file is opened read-only; every row is validated; you point it at a copy, never the live file |
| A hostile or damaged settings file (`stores.json`) | Strict validation: size limit, plain file only (no links), known keys only, names and types checked, control characters refused, duplicate names refused; a damaged file produces a clear message, never a crash. It is written atomically and privately |
| Finding likely stores reading things it should not | Discovery only lists folder names and looks for markdown files; it never reads file contents, refuses links and junctions, skips odd names, and offers at most 50 candidates. It asks before adding anything, unless you pass `--yes` |
| Docker doing more than memdebug needs | Exactly three fixed things are ever run: list containers, a fixed read-only SQLite backup script in an Open WebUI container, and copy that file out (then delete the temporary file). Commands are argument lists, never a shell; the container name must match a strict pattern, which also stops it being read as a Docker option; the script contains no input; docker is found only through absolute PATH entries; output, time and file size are limited |
| A failed or hostile copy replacing a good one | The copy must be a regular file of reasonable size that starts with SQLite's header, and replaces the old copy atomically; if any step fails the last good copy stays |
| Agent discovery reading credentials kept beside an agent's memory | Presence is judged by `lstat` of known folders only; no file is opened. Where memory is one file in a settings folder (`~/.gemini`, `~/.codex`, `~/.claude`) only that named file is ever listed or read (a test records every file opened and checks that credentials never are). Links and junctions are ignored. It never claims an app is installed when it cannot know |
| Provenance being trusted when it should not be, or reading private chat text | For Open WebUI, memdebug records the app's own label (`created_by`, `type`, shown only as short plain text, otherwise just its size) and how close the nearest chat messages were. From the chat records it reads only each message's time and role, never its text (a test checks the SQL). The label never decides the source kind or the trust: a memory labelled `manual` stays "unknown" in trust, because anything able to write the database could write that label. The wording is deliberately descriptive ("consistent with being added by hand"), never "was added by" |
| One failing store hiding a problem in another | Each store is checked independently; a store that cannot be read, or fails unexpectedly, is reported and the rest still run |

## Witness, reports and hints

| Threat | Defence |
| --- | --- |
| A rewritten or truncated ledger passing `verify` | The witness records the ledger's newest entry and hash in a separate, self-chaining file; `verify --witness` fails if the ledger no longer contains it. A broken ledger is never witnessed. Only as strong as the witness's separation from the ledger: a witness on the same disk is flagged as weak |
| A tampered witness file | Each line carries the hash of the previous line; lines must be in memdebug's exact form, in order; a damaged witness is reported and never extended |
| Writing through a link when saving a witness or report | Links, junctions and folders are refused; reports are never overwritten without `--force`; report files are created private and written atomically |
| Memory text turning into markup, links, HTML or terminal escapes in a report | Markdown puts memory text only inside code fences longer than any run of backticks in it, with control and bidirectional characters made visible; JSON and SARIF carry it as plain strings; SARIF only gives a file location to ids that look like file names |
| Hints being slow, being used to flood, or leaking secrets | Scanning is bounded in characters, per line and in time; at most 12 hints per text; excerpts are redacted of secret-like strings and made safe; a secret is reported only as its kind and length |
| Hints giving false confidence | They are labelled as guesses everywhere they appear, describe what a change added (or what a store holds when you first add it), never affect the default exit code, and a test keeps a list of wording they are known to miss |

## Known limits

Read these. They are why this is alpha software.

* **A git store with a very long history is only partly read.** memdebug reads at most 100,000 file changes and 100,000,000 characters of old and new text of a
  git store's history, oldest first, on every look. Every edit counts both its old and its new text, so a 20 KB note edited 2,500 times reaches the text limit
  before the row limit. Past either limit the newest history is missing and the check for notes edited outside git is **off**: such an edit is neither flagged
  nor recorded. `check` and `watch` say "NOT READ IN FULL" and `check` exits 1, so this is reported, but not yet fixed. A plain folder keeps no history and is not
  affected. The fix is to read only the commits since the last look; it is not built.
* **The stand-alone programs are not signed.** The GitHub release holds a program for Windows, Linux and macOS that needs no Python, built by the release
  workflow from the tagged source (PyInstaller, one file) after it passed a smoke test on that operating system. Each file has a checksum in `SHA256SUMS` and a
  build attestation, so you can check that it came from this repository's workflow (`gh attestation verify FILE --repo juraj-jumic/memdebug`). They carry no
  code-signing certificate, so Windows SmartScreen and macOS Gatekeeper may warn, and an antivirus may take a packed program for something else. Someone who
  can replace both the file and its checksum on your side of the download can still hand you a different program; the attestation is what ties a file to the
  workflow. PyPI is the other route (`pip install memdebug`) and has its own attestations.
* **No encryption at rest.** The ledger holds memory text, including deleted memories, and entries cannot be erased one
  by one without breaking the chain. Snapshots are as sensitive as the ledger. On Windows the tool does not enforce file
  permissions; the default ledger folder is private to your account.
* **Whoever can rewrite the whole ledger file can rebuild a valid chain**, unless you keep a witness somewhere they cannot reach
  (`memdebug witness`). With no witness, only `verify --expected-head` against a hash you saved yourself helps. There is no signing yet.
* **Provenance is a first slice, for Open WebUI only.** It records the app's own label and a timing comparison with chat messages. That is
  evidence, not proof: the label can be forged by anything that can write the database, and "a chat was active within 120 seconds" is a
  correlation. Nothing yet says which conversation turn wrote a memory or what the assistant had just read. For other agents the trust
  field stays `unknown`.
* **memdebug does not block attacks or inspect meaning.** Hints are heuristics on wording; they miss paraphrases and many languages and can
  flag harmless text. It is not a prompt-injection detector.
* **The agent catalog is only as right as the documentation it came from,** and locations change between versions. Anything not in it can be added with `memdebug add`.
* **Docker access is trust.** For an Open WebUI store in Docker, memdebug runs a fixed read-only script inside the container through your Docker
  installation. It cannot do more than you could already do with `docker exec`, but it does require that access.
* **Stores without a history cannot show a bypass.** For a plain folder or Open WebUI memdebug sees only the state at each look, so a change
  made and reverted between looks is invisible, and an attacker's edit looks like any other. Prefer a git repository when you can.
* **Rollback for Open WebUI and Mem0 does not exist** (memdebug only reads those). Plain folders and git notes can be rolled back.
* **Rollback restores files, not the world.** It does not change what a *running* agent has already loaded (restart the
  session), does not rebuild anything derived from the memory (summaries, embeddings), cannot undo what the agent did
  because of a bad memory, and a still-running agent can write the same text back. Stop the agent first.
* A change made and reverted between two syncs is invisible. A snapshot is the listing at one moment; a write during the
  listing can be half-seen.
* **git:** only the checked-out branch is read, along first parents. Files over 1 MiB are compared by size only. Line
  endings are normalised, so a change that only flips them is invisible, and a restored file is written exactly as git
  stores it (with `autocrlf` on, git still reports it clean). git 2.31+ is required, and git runs with the untrusted
  repository as its working folder, so keep git up to date.
* On Windows `O_NOFOLLOW` does not exist, so the link check relies on `lstat` and the reparse-point attribute, with a
  tiny window between check and open.
* The viewer is plain HTTP on your own machine; the first link, which contains the secret, stays in browser history.
  Do not paste it into chats or screenshots.
* `adopt_existing` trusts the store as it is on the first run.

## How this is verified

* The test suite (hostile inputs throughout, including real symlinks, junctions, hooks and filters), run on Linux, Windows and
  macOS by CI on every push, and by the author on Windows 11.
* Deliberate sabotage: for the rollback engine, each protection above was removed in turn to confirm a test fails.
  A protection that no test notices is treated as missing.
* `memdebug selftest`, which demonstrates the platform-dependent protections on the machine in front of you.
