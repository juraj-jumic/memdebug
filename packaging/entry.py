"""Entry point of the stand-alone build: the `memdebug` command, packed with its own Python so nothing needs installing.

Built by `python -m PyInstaller --onefile --name memdebug packaging/entry.py` (see docs/releasing.md).
"""
import sys

from memdebug.cli import app
from memdebug.standalone import explain_if_double_clicked

if __name__ == "__main__":
    if explain_if_double_clicked():  # opened by double-click: say what to do instead of flashing the help away
        sys.exit(0)
    app()
