"""Render a Plan: per-clip segment files in parallel, then concat (SPEC §2.1, §8)."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from fractions import Fraction
from pathlib import Path
from typing import Callable

from ..config import RuntimeConfig
from ..errors import Cancelled, RenderError
from ..media.probe import VideoInfo
from ..media.tools import Tools
from ..planner.plan import ClipSpec, Plan
from ..planner.planner import reselect_source
from ..util.process import run_capture, run_cancellable
from ..util.progress import ProgressCallback, null_progress
from .frame_segment import render_frame_segment
from .ffmpeg import EncodeSettings, build_chain, concat_command, segment_command

log = logging.getLogger("moviekaleidoscope")


class _AnyEvent:
    """is_set() is true when any of the wrapped events is set."""

    def __init__(self, *events: threading.Event | None) -> None:
        self._events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def default_jobs() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def _tail(text: str, n: int = 6) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:]) or "(ffmpeg からのメッセージなし)"


def count_frames(tools: Tools, path: Path) -> int | None:
    """Frame count from the container header (no decoding)."""
    code, out, _ = run_capture([tools.ffprobe, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=nb_frames", "-of", "csv=p=0", "-i", str(path)],
                               timeout=30)
    text = out.strip().splitlines()[0].strip() if out.strip() else ""
    return int(text) if code == 0 and text.isdigit() else None


def render_segment(tools: Tools, clip: ClipSpec, plan: Plan, enc: EncodeSettings,
                   dest: Path, threads: int, cancel: _AnyEvent | None) -> None:
    """Render one clip to `dest`; raises RenderError unless exactly clip.frames frames result."""
    chain = build_chain(clip, plan)
    if chain.uses_frame_backend:
        render_frame_segment(tools, clip, plan, enc, dest, threads, chain, cancel)
    else:
        rc, err = run_cancellable(segment_command(tools, clip, plan, enc, dest, threads), cancel)
        if rc != 0:
            raise RenderError(f"クリップ {clip.index} の書き出しに失敗しました({clip.source.path}):\n{_tail(err)}")
    n = count_frames(tools, dest)
    if n is None:
        log.warning("クリップ %d: フレーム数を確認できません", clip.index)
    elif n != clip.frames:
        raise RenderError(f"クリップ {clip.index}: フレーム数が一致しません(期待 {clip.frames}、実際 {n}、素材 {clip.source.path})")


def _make_work_dir(runtime: RuntimeConfig) -> Path:
    if runtime.work_dir is not None:
        runtime.work_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="mkaleido_", dir=runtime.work_dir))
    return Path(tempfile.mkdtemp(prefix="mkaleido_"))


def _remove_work_dir(work: Path) -> None:
    """Delete the work folder; say so if that fails (it used to fail silently, which hid ffmpeg
    processes that survived a cancel and kept their segment files open)."""
    try:
        shutil.rmtree(work)
    except OSError as e:
        if work.exists():
            log.warning("作業フォルダを削除できませんでした: %s(%s)", work, e)


def render_plan(
    plan: Plan,
    *,
    tools: Tools,
    enc: EncodeSettings,
    runtime: RuntimeConfig,
    output: Path,
    progress: ProgressCallback = null_progress,
    cancel: threading.Event | None = None,
    inputs: list[VideoInfo] | None = None,
    segment_renderer: Callable[..., None] = render_segment,
) -> list[int]:
    """Render `plan` into `output` (MP4). Returns indices of clips whose source was replaced.

    When `inputs` is given, a failed clip is retried once with a different input
    (SPEC §3.2) and the plan is updated in place; otherwise any failure is fatal.
    """
    jobs = runtime.jobs or default_jobs()
    jobs = max(1, min(jobs, len(plan.clips)))
    threads = max(1, (os.cpu_count() or 1) // jobs)
    stop = threading.Event()
    token = _AnyEvent(cancel, stop)
    replaced: list[int] = []
    lock = threading.Lock()

    work = _make_work_dir(runtime)
    log.debug("作業フォルダ: %s", work)
    try:
        seg_paths = [work / f"seg_{c.index + 1:05d}.mp4" for c in plan.clips]

        def task(clip: ClipSpec) -> None:
            dest = seg_paths[clip.index]
            try:
                segment_renderer(tools, clip, plan, enc, dest, threads, token)
            except RenderError as e:
                if inputs is None or token.is_set():
                    raise
                new = reselect_source(plan, clip, inputs)
                if new is None:
                    raise
                log.warning("%s\n別の入力で 1 回だけやり直します: %s", e, new.path)
                clip.source = new
                with lock:
                    replaced.append(clip.index)
                segment_renderer(tools, clip, plan, enc, dest, threads, token)

        total = plan.output.frames      # progress in frames: clips differ a lot in length
        progress("セグメント生成", 0, total)
        pool = ThreadPoolExecutor(max_workers=jobs)
        try:
            futures = {pool.submit(task, c): c for c in plan.clips}
            pending = set(futures)
            done_frames = 0
            while pending:
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_EXCEPTION)
                for f in done:
                    f.result()  # re-raises the first failure
                    done_frames += futures[f].frames
                if done:
                    progress("セグメント生成", done_frames, total)
        except BaseException:
            stop.set()
            for f in futures:
                f.cancel()
            raise
        finally:
            pool.shutdown(wait=True)

        _concat(tools, seg_paths, work, output, plan, token, progress)
    finally:
        if runtime.keep_temp:
            log.info("作業フォルダを残しました: %s", work)
        else:
            _remove_work_dir(work)
    return sorted(replaced)


def _concat(tools: Tools, seg_paths: list[Path], work: Path, output: Path, plan: Plan,
            token: _AnyEvent, progress: ProgressCallback) -> None:
    progress("連結", 0, 1)
    list_file = work / "concat.txt"
    # Relative ASCII names only, so non-ASCII output paths never reach the list file.
    #
    # Each file gets an explicit `duration` taken from the plan. Without it ffmpeg reads the
    # duration from the MP4 header, which stores it in milliseconds; the rounding error adds up
    # at every cut (about 0.3 ms each: 0.2 s over ~600 cuts, enough to drift off the music).
    # Durations are differences of rounded *cumulative* times, so they never accumulate error.
    fps = plan.output.fps

    def micros(frames: int) -> int:
        return round(Fraction(frames) / fps * 1_000_000)

    lines = []
    for clip, p in zip(plan.clips, seg_paths):
        d = micros(clip.start_frame + clip.frames) - micros(clip.start_frame)
        lines.append(f"file '{p.name}'\nduration {d // 1_000_000}.{d % 1_000_000:06d}\n")
    list_file.write_text("".join(lines), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    try:
        rc, err = run_cancellable(concat_command(tools, list_file, partial, plan), token)
        if rc != 0:
            raise RenderError(f"連結に失敗しました:\n{_tail(err)}")
        _check_duration(tools, partial, plan)
        os.replace(partial, output)
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)
    progress("連結", 1, 1)


def _check_duration(tools: Tools, path: Path, plan: Plan) -> None:
    code, out, _ = run_capture([tools.ffprobe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", "-i", str(path)], timeout=30)
    try:
        actual = float(out.strip())
    except ValueError:
        log.warning("出力の長さを確認できません")
        return
    frame = max(float(1 / plan.output.fps), 0.05)     # AAC adds up to one 1024-sample packet
    if abs(actual - plan.output.duration) > frame:
        log.warning("出力の長さが plan と異なります(plan %.3f 秒 / 実際 %.3f 秒)", plan.output.duration, actual)
