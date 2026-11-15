"""Subprocess helpers. Never uses a shell; arguments are always a list."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Protocol, Sequence

from ..errors import Cancelled


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


def run_capture(
    args: Sequence[str | os.PathLike],
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr) decoded as UTF-8.

    Raises subprocess.TimeoutExpired / OSError on failure to run.
    """
    cp = subprocess.run(
        [os.fspath(a) for a in args],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    return (
        cp.returncode,
        cp.stdout.decode("utf-8", "replace"),
        cp.stderr.decode("utf-8", "replace"),
    )


def run_bytes(args: Sequence[str | os.PathLike], timeout: float | None = None) -> tuple[int, bytes, str]:
    """Like run_capture, but stdout is returned as raw bytes (e.g. decoded audio)."""
    cp = subprocess.run([os.fspath(a) for a in args], capture_output=True,
                        stdin=subprocess.DEVNULL, timeout=timeout)
    return cp.returncode, cp.stdout, cp.stderr.decode("utf-8", "replace")


# Windows: ffmpeg runs in its own process group, so a console Ctrl+C reaches only us and we stop
# it ourselves (terminate()). Otherwise ffmpeg died first, and a worker took that for a failed
# clip -- warned and retried it with another input -- before the main thread saw the interrupt.
CHILD_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def terminate(proc: subprocess.Popen) -> None:
    """Stop a child and wait for it.

    On Windows this stops the whole process tree: the ffmpeg on PATH can be a launcher (the
    Chocolatey package installs a shim that starts the real ffmpeg.exe as its child), and
    terminate() alone stopped only the launcher, leaving ffmpeg running and its file locked."""
    if proc.poll() is not None:
        return
    taskkill = shutil.which("taskkill") if os.name == "nt" else None
    if taskkill:
        subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_cancellable(
    args: Sequence[str | os.PathLike],
    cancel: CancelToken | None = None,
    poll: float = 0.1,
) -> tuple[int, str]:
    """Run a command to completion; returns (returncode, stderr text).

    stdout is discarded. If `cancel` becomes set (or KeyboardInterrupt arrives)
    the child is terminated and Cancelled / the interrupt propagates.
    """
    if cancel is not None and cancel.is_set():
        raise Cancelled("中断しました")  # do not even start the process
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            [os.fspath(a) for a in args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            creationflags=CHILD_FLAGS,
        )
        try:
            while True:
                try:
                    rc = proc.wait(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("中断しました") from None
        except BaseException:
            terminate(proc)
            raise
        err.seek(0)
        text = err.read().decode("utf-8", "replace")
    return rc, text
