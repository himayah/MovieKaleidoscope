"""Run one job (generate / re-render) in a background thread and report back through a queue.

No tkinter here: the GUI polls `poll()` from its own thread, and tests drive it directly.
Events are (kind, payload):
    ("progress", (stage, current, total))
    ("log", (levelno, text))
    ("done", result)                      result of the job
    ("failed", (exit_code, message, traceback_text_or_None))
    ("cancelled", None)
Exactly one of done / failed / cancelled ends every job.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from ..api import generate, render_saved_plan
from ..config import Config
from ..errors import Cancelled, MkError
from ..util.progress import ProgressCallback

Event = tuple[str, Any]
Job = Callable[[ProgressCallback, threading.Event], Any]

LOGGER = "moviekaleidoscope"


class _QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[Event]") -> None:
        super().__init__(logging.INFO)
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(("log", (record.levelno, record.getMessage())))
        except Exception:      # a log line must never break a job
            pass


def generate_job(cfg: Config, plan_only: bool = False) -> Job:
    return lambda progress, cancel: generate(cfg, plan_only=plan_only, progress=progress, cancel=cancel)


def replay_job(plan_path: Path, cfg: Config) -> Job:
    return lambda progress, cancel: render_saved_plan(plan_path, cfg, progress, cancel)


class JobRunner:
    def __init__(self) -> None:
        self._events: "queue.Queue[Event]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, job: Job) -> None:
        if self.running:
            raise RuntimeError("別の処理が実行中です")
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(job, self._cancel), daemon=True, name="mkaleido-job")
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.running

    def poll(self) -> list[Event]:
        """All events since the last call (never blocks)."""
        out: list[Event] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ---- worker thread ---------------------------------------------------------------------------------
    def _run(self, job: Job, cancel: threading.Event) -> None:
        logger = logging.getLogger(LOGGER)
        handler = _QueueHandler(self._events)
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(min(old_level or logging.INFO, logging.INFO))
        put = self._events.put
        try:
            result = job(lambda stage, cur, tot: put(("progress", (stage, cur, tot))), cancel)
        except Cancelled:
            put(("cancelled", None))
        except MkError as e:
            put(("failed", (e.exit_code, str(e), None)))
        except Exception as e:     # a bug: show it, with the trace for a report
            put(("failed", (1, f"予期しないエラー: {type(e).__name__}: {e}", traceback.format_exc())))
        else:
            put(("done", result))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
