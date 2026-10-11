# Stability and compatibility

From 1.0.0 memdebug follows [Semantic Versioning](https://semver.org/): a change that breaks something listed as stable below only comes in a
new major version, new things arrive in minor versions, and fixes in patch versions. This page says what is stable, what is not, and how the
ledger (the one thing you cannot regenerate) is protected across versions.

## What is stable

| What | The promise |
| --- | --- |
| The commands and options shown by `memdebug --help` and in [usage.md](usage.md) | Not removed or renamed, and no option changes meaning, within a major version. New commands and options can appear in a minor version. A feature to be removed is announced in the changelog and warns when used for at least one minor version first. |
| Exit codes | `0` nothing needs a look. `1` something needs a look, or a check failed, or a store was not read in full, or the person said no (`check`, `verify`, `report --fail-on`, `selftest`, a `snapshot` refused until alarms are looked at, a blocked or cancelled `rollback`, a declined `autostart`, a failed `backups clean`). `2` an error memdebug understood (a bad path, nothing watched yet, a damaged settings file). `3` a rollback changed the files but could not record it in the ledger. `70` an unexpected internal error. |
| The ledger file | See "The ledger" below. |
| The JSON report (`memdebug report --format json`) | The keys that exist are not removed or changed in meaning within a major version. New keys may be added in a minor version, so read the keys you need and ignore the rest. The document carries `schema_version` (1); a breaking change raises it and only comes in a major version. |
| The SARIF report (`--format sarif`) | SARIF 2.1.0. The rule ids `outside-history`, `untrusted-source`, `integrity`, `hint` and `witness` stay; new ids may be added. |
| The settings file (`stores.json`, `"version": 1`) | An older memdebug reads every file a newer one wrote in version 1 only if it uses no option the older one lacks; it **refuses** a file with an option it does not know instead of guessing. A newer memdebug always reads an older file. |
| The witness file | One JSON line per entry with `"v": 1`; lines are never rewritten. |
| The folder of rollback backups | `backups/<store>/<UTC time>-<8 hex digits>/`, which is what `memdebug backups list` and `clean` rely on. |

## What is not stable

* **The words `check`, `watch`, `status` and the other commands print.** They are for people and get reworded. To read results in a program use
  the JSON report and the exit codes.
* **The browser viewer**: its pages, addresses and HTML. It is a tool for looking, not an interface.
* **`status.json` and the `watch` log file** that `check` and `watch` leave beside the ledger for the viewer and for a background watcher. They are
  conveniences, written as plain data, and may change in any release.
* **The Python modules.** memdebug is a program, not a library; nothing is imported by anyone else on a promise.
* The hidden `selftest-helper` command and the `packaging/` scripts.

## The ledger

The ledger is a single SQLite file. Everything memdebug has recorded lives in it, and it cannot be regenerated, so its format is the most
carefully protected part.

* **Version.** The file's `PRAGMA user_version` is the format version. It has been `3` in every release since 0.2.0, the first. Versions `1` and `2`
  only ever existed during development and are upgraded in place if one is found.
* **Compatibility, tested.** `tests/data/ledgers/` holds ledgers made by real releases (0.2.0, 0.3.0, 0.4.1 and 0.6.1). Every test run opens each
  one with the current code, verifies it, reads it back, appends new entries to it and checks the whole chain again. The hash format and the
  text that gets hashed are pinned by tests that fail on purpose if anyone changes them.
* **Newer than the program.** An older memdebug refuses a ledger with a format version it does not know, with a clear message; it never guesses
  and never writes to it.
* **Upgrades.** Within a major version a format change is only additive, and applied in place, in one transaction, by the first command that
  writes to the ledger; a read-only open (the viewer) never upgrades and says to run any normal command. Before a **major** version, copy the
  file once. Nothing in memdebug ever deletes or rewrites recorded entries.
* **The chain.** Each entry is a row in `events` with its `payload` (the canonical JSON of the event: keys sorted, no spaces) and
  `hash = SHA-256("<previous hash>|<entry id>|<payload>")`. The first entry's previous hash is 64 zeros. Changing, removing or reordering a
  recorded entry breaks every hash after it. Cutting entries off the **end** is not visible from the file alone: that is what the witness is for.

### Checking a ledger without memdebug

You do not have to trust memdebug to check its own ledger. This checks the chain with nothing but Python's standard library
(run it as `python check.py ledger.db`):

```python
import hashlib
import sqlite3
import sys

db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
assert db.execute("PRAGMA user_version").fetchone()[0] == 3, "not a version 3 ledger"
previous = "0" * 64
count = 0
for seq, entry_id, payload, prev_hash, digest in db.execute("SELECT seq, id, payload, prev_hash, hash FROM events ORDER BY seq"):
    assert prev_hash == previous, f"entry {seq}: the chain is broken before it"
    assert digest == hashlib.sha256(f"{prev_hash}|{entry_id}|{payload}".encode()).hexdigest(), f"entry {seq}: its content was changed"
    previous = digest
    count += 1
print(f"{count} entries, the chain is intact; its newest hash is {previous}")
```

(`memdebug verify` does this and more: it also checks the stored snapshots against the chain and every index column against its entry.)

## Platforms and Python

memdebug supports Python 3.10 and newer, and runs its tests on every push on Linux, Windows and macOS (Python 3.10, 3.12 and 3.14). Support for a
Python version ends no sooner than a year after that version stops getting upstream security fixes, and only in a minor or major release, announced in the
changelog. The stand-alone programs need no Python. Which agents have been tried on a real installation, and which only follow the agent's
documentation, is in [agents.md](agents.md).

## Reporting a problem

Security problems: see [SECURITY.md](../SECURITY.md). Anything else: an issue on the repository. If an upgrade ever fails to read your ledger,
keep the file, tell us the versions involved, and do not delete it: a ledger from any released version is meant to keep working.
