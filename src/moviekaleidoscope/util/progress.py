"""Progress reporting shared by the CLI and (later) the GUI.

A ProgressCallback is called as callback(stage, current, total). Stages used by the core:
入力検証 (files), BPM 解析, 計画, セグメント生成 (frames), 連結.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

ProgressCallback = Callable[[str, int, int], None]

STAGE_UNITS = {"入力検証": "件", "セグメント生成": "フレーム"}


def null_progress(stage: str, current: int, total: int) -> None:
    """Progress callback that does nothing."""


class TqdmProgress:
    """One tqdm bar per stage on stderr (for a terminal)."""

    def __init__(self, file=None) -> None:
        self._file = file
        self._bar = None
        self._stage: str | None = None

    def __call__(self, stage: str, current: int, total: int) -> None:
        from tqdm import tqdm

        if stage != self._stage:
            self.close()
            self._stage = stage
            self._bar = tqdm(total=total, desc=stage, unit=STAGE_UNITS.get(stage, ""), unit_scale=False,
                             file=self._file or sys.stderr, dynamic_ncols=True, leave=False, mininterval=0.2)
        self._bar.n = current
        self._bar.refresh()

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


class LogProgress:
    """Progress as log lines (for redirected output, where a bar would only make noise):
    one line when a stage starts, one every `step` of it, one when it ends."""

    def __init__(self, log: logging.Logger, step: float = 0.25) -> None:
        self._log, self._step = log, step
        self._stage: str | None = None
        self._next = 0.0

    def __call__(self, stage: str, current: int, total: int) -> None:
        unit = STAGE_UNITS.get(stage, "")
        if stage != self._stage:
            self._stage, self._next = stage, self._step
            if total > 1:
                self._log.info("%s: 開始(%d%s)", stage, total, unit)
            else:
                self._log.info("%s", stage)
            return
        if total > 1 and current < total and current >= self._next * total:
            self._log.info("%s: %d%%(%d/%d%s)", stage, round(100 * current / total), current, total, unit)
            while self._next <= current / total:
                self._next += self._step
        elif total > 1 and current >= total:
            self._log.info("%s: 完了", stage)
