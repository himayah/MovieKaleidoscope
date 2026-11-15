"""Press Ctrl+C on a running command, for real, on Windows (TESTING.md W-B2 (2)).

    python tests/tools/ctrl_c_windows.py DELAY COMMAND [ARGS...]

The command runs in a NEW console. After DELAY seconds a CTRL_C_EVENT is sent to every process
attached to that console -- the command and all its children, as when a user presses Ctrl+C --
and the console that launched this script never sees it. Prints the exit code, how long the
command took to stop, the ffmpeg.exe processes before and after, and the command's output.
"""

from __future__ import annotations

import ctypes
import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _ffmpeg_count() -> int:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if line.lower().startswith('"ffmpeg.exe"'))


def _child(result: Path, delay: float, cmd: list[str]) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)          # this script survives its own event
    with open(result.with_suffix(".log"), "wb") as log:
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        time.sleep(delay)
        before = _ffmpeg_count()
        t0 = time.monotonic()
        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0)   # CTRL_C_EVENT, whole console
        try:
            rc = p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = "timeout"
        stop = time.monotonic() - t0
    time.sleep(1)
    result.write_text(json.dumps({"exit_code": rc, "stop_seconds": round(stop, 2),
                                  "ffmpeg_before": before, "ffmpeg_after_1s": _ffmpeg_count()}), encoding="utf-8")


def main() -> int:
    if sys.platform != "win32":
        sys.exit("Windows only")
    if len(sys.argv) >= 2 and sys.argv[1] == "--child":
        _child(Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4:])
        return 0
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    result = Path(tempfile.mkdtemp(prefix="ctrlc_")) / "result.json"
    subprocess.run([sys.executable, __file__, "--child", str(result), *sys.argv[1:]],
                   creationflags=subprocess.CREATE_NEW_CONSOLE)
    print(result.read_text(encoding="utf-8"))
    print(result.with_suffix(".log").read_bytes().decode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
