"""Graphical front end (tkinter). Start it with `mkaleido-gui` or `python -m moviekaleidoscope.gui`."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from .app import main as run
    except ImportError as e:              # tkinter is optional on some Linux installs
        print("GUI には tkinter が必要です(" + str(e) + ")。\n"
              "  Windows / macOS: 公式の Python のインストーラーに含まれています。\n"
              "  Debian / Ubuntu: sudo apt install python3-tk\n"
              "コマンドライン版(mkaleido)は tkinter なしで使えます。", file=sys.stderr)
        return 1
    return run(argv)
