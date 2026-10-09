# Rolling back

How to put a watched store back to a snapshot, and what each rollback guarantees.

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
