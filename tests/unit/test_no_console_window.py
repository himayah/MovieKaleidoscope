"""Children must not get a console window of their own (Windows).

mkaleido-gui.exe has no console, so every ffprobe / ffmpeg / taskkill it started opened an
empty black terminal window (C1, 2026-11-16). The helpers now pass CREATE_NO_WINDOW.

The check runs the helpers inside a parent with no console at all (pythonw, detached), like the
GUI: a child that gets a console window reports a non-zero GetConsoleWindow(). Running them
straight from pytest would prove nothing, since pytest may itself sit in a hidden console that
children inherit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows thing")

PROBE = "import ctypes, sys; sys.stderr.write(str(ctypes.windll.kernel32.GetConsoleWindow()))"

PARENT = r"""
import sys
from moviekaleidoscope.util.process import run_bytes, run_cancellable, run_capture
probe = [sys.executable.replace("pythonw.exe", "python.exe"), "-c", {probe!r}]
results = [
    run_capture(probe, timeout=30)[2],
    run_bytes(probe, timeout=30)[2],
    run_cancellable(probe)[1],
]
open({out!r}, "w").write(",".join(results))
"""


def test_children_of_a_console_less_parent_get_no_console_window(tmp_path):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pytest.skip("pythonw.exe not found next to the interpreter")
    out = tmp_path / "result.txt"
    script = PARENT.format(probe=PROBE, out=str(out))
    subprocess.run([str(pythonw), "-c", script], timeout=60, check=True,
                   creationflags=subprocess.DETACHED_PROCESS)
    assert out.read_text() == "0,0,0"      # run_capture, run_bytes, run_cancellable
