"""Entry point of the stand-alone build: the `memdebug` command, packed with its own Python so nothing needs installing.

Built by `python -m PyInstaller --onefile --name memdebug packaging/entry.py` (see docs/releasing.md).
"""
from memdebug.cli import app

if __name__ == "__main__":
    app()
