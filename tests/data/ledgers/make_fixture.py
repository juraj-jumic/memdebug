"""Make a small ledger with whatever memdebug version runs this script, so later versions can be tested against real old ledgers.

Run it with the Python of an INSTALLED RELEASE (for example a clean virtual environment holding `memdebug==0.4.1`), never with this checkout:

    <venv>/python make_fixture.py OUTPUT.sql

It builds a throw-away notes folder of made-up notes, watches it, and changes it step by step (an edit, an addition, a removal, a
change that bypasses nothing because a folder has no history), checking after each step, then writes the ledger as plain SQL text
(`sqlite3` dump) with a first comment line naming the version and the number of ledger entries. Nothing here touches your own
memdebug settings: every file lives in a temporary folder.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args, env):
    result = subprocess.run([sys.executable, "-m", "memdebug", *args], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode


def main(out):
    import memdebug

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        home = tmp / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "local"), XDG_DATA_HOME=str(home / "data"))
        notes, db = tmp / "notes", tmp / "ledger.db"
        notes.mkdir()
        (notes / "tea.md").write_text("likes tea\n", encoding="utf-8")
        (notes / "pets.md").write_text("has a dog called Rex\n", encoding="utf-8")
        run("add", str(notes), "--name", "notes", "--db", str(db), env=env)
        run("check", "--db", str(db), "--settle", "0", env=env)
        (notes / "tea.md").write_text("likes green tea\n", encoding="utf-8")
        run("check", "--db", str(db), "--settle", "0", env=env)
        (notes / "plants.md").write_text("waters the fern on Sundays\n", encoding="utf-8")
        run("check", "--db", str(db), "--settle", "0", env=env)
        (notes / "pets.md").unlink()
        run("check", "--db", str(db), "--settle", "0", env=env)
        run("snapshot", "store", "notes", "--db", str(db), env=env)  # absent before 0.3.0: its failure is fine
        con = sqlite3.connect(db)
        entries = con.execute("SELECT count(*) FROM events").fetchone()[0]
        version = con.execute("PRAGMA user_version").fetchone()[0]
        text = "\n".join(con.iterdump())
        con.close()
    header = f"-- memdebug {memdebug.__version__}; user_version={version}; events={entries}\n"
    Path(out).write_text(header + text + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"version": memdebug.__version__, "user_version": version, "events": entries}))


if __name__ == "__main__":
    main(sys.argv[1])
