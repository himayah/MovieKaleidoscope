"""Temporarily switch to the "High performance" Windows power plan while rendering.

Rendering is CPU/GPU-bound; the default "Balanced" plan can throttle clock speed. This is a
best-effort optimisation: any failure (no `powercfg`, non-Windows, a plan that cannot be read)
just skips the switch rather than failing the render.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess

from .process import NO_WINDOW

log = logging.getLogger("moviekaleidoscope")

HIGH_PERFORMANCE_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@contextlib.contextmanager
def high_performance_power_plan(enabled: bool = True):
    """Switch to the High performance scheme for the duration of the `with` block.

    Switches back to whatever scheme was active before, even on an exception. No-op on
    non-Windows, when `enabled` is False, or when the scheme cannot be read/changed."""
    if not enabled or os.name != "nt":
        yield
        return
    original = _get_active_scheme()
    switched = original is not None and original != HIGH_PERFORMANCE_GUID and _set_scheme(HIGH_PERFORMANCE_GUID)
    try:
        yield
    finally:
        if switched:
            _set_scheme(original)


def _get_active_scheme() -> str | None:
    try:
        cp = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                            timeout=5, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("電源プランを確認できませんでした(powercfg を実行できません)")
        return None
    if cp.returncode != 0:
        return None
    m = _GUID_RE.search(cp.stdout.decode("utf-8", "replace"))
    return m.group(0) if m else None


def _set_scheme(guid: str) -> bool:
    try:
        cp = subprocess.run(["powercfg", "/s", guid], capture_output=True,
                            timeout=5, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("電源プランを切り替えられませんでした(powercfg を実行できません)")
        return False
    if cp.returncode != 0:
        log.warning("電源プランを切り替えられませんでした(%s)", cp.stderr.decode("utf-8", "replace").strip())
        return False
    return True
