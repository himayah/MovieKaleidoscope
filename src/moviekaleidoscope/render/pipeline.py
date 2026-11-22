"""Render a Plan: per-clip segment files in parallel, then concat (SPEC §2.1, §8)."""

from __future__ import annotations

import ctypes
import dataclasses
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
from ..media.probe import VideoInfo, probe_video
from ..media.tools import Tools
from ..effects import registry
from ..planner.plan import ClipSpec, Plan
from ..planner.planner import reselect_source
from ..util.memory import available_bytes
from ..util.process import run_capture, run_cancellable
from ..util.progress import ProgressCallback, null_progress
from .frame_segment import FRAME_QUEUE_MAXSIZE, render_frame_segment
from .ffmpeg import EncodeSettings, NVENC_PRESET, build_chain, concat_command, segment_command

log = logging.getLogger("moviekaleidoscope")


class _AnyEvent:
    """is_set() is true when any of the wrapped events is set."""

    def __init__(self, *events: threading.Event | None) -> None:
        self._events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def default_jobs(backend: str = "software", frame_backend: bool = False) -> int:
    """CPU-based default parallelism.

    `backend == "nvenc"` frees the CPU from libx264 encoding, so more jobs fit -- but only when
    no clip uses the Python frame backend: its per-frame orchestration holds the GIL, so more
    threads do not reliably speed it up (SPEC: GPU redesign review finding, 2026-11-21)."""
    cpu = os.cpu_count() or 2
    if backend == "nvenc" and not frame_backend:
        return max(1, cpu * 3 // 4)
    return max(1, cpu // 2)


def _uses_frame_backend(plan: Plan) -> bool:
    """Whether any clip needs the Python frame backend (not just ffmpeg filters).

    Goes through the registry (`backend_for`), not `build_chain()`: the latter needs a fully
    sampled RenderContext and motion params for every effect, which this check should not
    require (e.g. it also runs from tests that build a Plan with bare, unsampled EffectSpecs)."""
    return any(registry.get(e.type).backend_for(e.params) == "frame"
              for c in plan.clips for e in c.effects)


# Memory of one running segment (ffmpeg decoding + encoding, plus the Python frame pipeline).
# Measured on Windows, 2026-11-16, at jobs=12: 4K sources (8.3 MP) took 17.2 GB in all, about
# 1.4 GB per job; 720p sources (0.9 MP) 2.1 GB, about 0.17 GB per job. The decoder dominates.
JOB_BASE_BYTES = 100 * 2**20
JOB_BYTES_PER_SOURCE_PIXEL = 170
MEMORY_SHARE = 0.7               # leave the rest of the free memory to the system and other apps


def job_memory_estimate(source_pixels: int, plan: Plan) -> int:
    """Bytes one job may need. `reverse` holds its whole clip (yuv420p, up to 10 s) in memory."""
    est = JOB_BASE_BYTES + JOB_BYTES_PER_SOURCE_PIXEL * source_pixels
    if any(e.type == "reverse" for c in plan.clips for e in c.effects):
        out = plan.output
        longest = max(c.frames for c in plan.clips)
        est += int(out.width * out.height * 1.5 * longest)
    if _uses_frame_backend(plan):
        out = plan.output
        est += FRAME_QUEUE_MAXSIZE * out.width * out.height * 3   # the frame reader's RGB24 lookahead
    return est


def auto_jobs(source_pixels: int, plan: Plan, available: int | None,
              backend: str = "software", frame_backend: bool | None = None) -> int:
    """CPU-based default, lowered so that the jobs together fit in the free memory."""
    if frame_backend is None:
        frame_backend = _uses_frame_backend(plan)
    jobs = default_jobs(backend, frame_backend)
    if available is None:
        return jobs
    fit = max(1, int(available * MEMORY_SHARE // job_memory_estimate(source_pixels, plan)))
    return min(jobs, fit)


# ---------------------------------------------------------------------------------------------
# GPU encoder backend (SPEC: GPU redesign §A). Cached per ffmpeg path: the probe briefly
# initialises the GPU encoder, not worth repeating on every render_plan() call in a long-lived
# process (e.g. the GUI).
# ---------------------------------------------------------------------------------------------
_backend_cache: dict[str, str] = {}


def resolve_encoder_backend(tools: Tools, requested: str) -> str:
    """Resolve "auto" to "software"/"nvenc"; an explicit request passes through unchanged."""
    if requested != "auto":
        return requested
    cached = _backend_cache.get(tools.ffmpeg)
    if cached is None:
        cached = _probe_nvenc(tools)
        _backend_cache[tools.ffmpeg] = cached
    return cached


def _probe_nvenc(tools: Tools) -> str:
    code, out, _ = run_capture([tools.ffmpeg, "-hide_banner", "-encoders"], timeout=10)
    if code != 0 or "h264_nvenc" not in out:
        return "software"
    try:
        code, _, _ = run_capture(
            [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x256:d=0.2", "-frames:v", "4",
             "-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-f", "null", "-"],
            timeout=10)
    except (OSError, TimeoutError):
        code = 1
    if code != 0:
        log.warning("NVENC を初期化できないため software エンコードを使います")
        return "software"
    return "nvenc"


# ---------------------------------------------------------------------------------------------
# Work directory placement (SPEC: GPU redesign §D). This is a free-space safeguard, not a speed
# optimisation: %TEMP%'s own drive is kept unless it is nearly full, in which case the fixed
# drive with the most free space is used instead. Cached per process (drives don't change).
# ---------------------------------------------------------------------------------------------
MIN_FREE_BYTES = 20 * 2**30   # 20GB; below this, switch away from %TEMP%'s drive
DRIVE_FIXED = 3
_UNSET = object()
_work_drive_cache: Path | None | object = _UNSET


def _fixed_drive_roots() -> list[Path]:
    """Roots of locally-attached fixed drives (Windows only; empty elsewhere)."""
    if os.name != "nt":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    roots = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = f"{chr(65 + i)}:\\"
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
                roots.append(Path(root))
        except OSError:
            continue
    return roots


def _pick_fastest_free_drive(candidates: list[Path]) -> Path | None:
    """The candidate with the most free space (None if none can be read)."""
    best, best_free = None, -1
    for root in candidates:
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if free > best_free:
            best, best_free = root, free
    return best


def _choose_work_drive() -> Path | None:
    """None outside Windows: `Path.anchor` is always "/" on POSIX (one filesystem root, not a
    per-drive letter), so this drive-letter logic doesn't apply there -- `_make_work_dir()` then
    falls back to the plain system temp dir, as it always did before this optimisation."""
    global _work_drive_cache
    if _work_drive_cache is not _UNSET:
        return _work_drive_cache
    if os.name != "nt":
        _work_drive_cache = None
        return None
    temp_drive = Path(tempfile.gettempdir()).anchor
    try:
        temp_free = shutil.disk_usage(temp_drive).free
    except OSError:
        temp_free = -1
    drive = Path(temp_drive) if temp_free >= MIN_FREE_BYTES else _pick_fastest_free_drive(_fixed_drive_roots())
    _work_drive_cache = drive
    return drive


def _largest_source_pixels(plan: Plan, tools: Tools, inputs: list[VideoInfo] | None) -> int:
    if inputs:
        return max(v.width * v.height for v in inputs)
    best = 0                                       # --from-plan: probe the few files it uses
    for path in {c.source.path for c in plan.clips}:
        try:
            v = probe_video(path, tools)
        except Exception:                          # noqa: BLE001 - only an estimate; render reports real errors
            continue
        best = max(best, v.width * v.height)
    return best or plan.output.width * plan.output.height


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


# ---------------------------------------------------------------------------------------------
# NVENC concurrent-session backoff (SPEC: GPU redesign §A). A GPU's driver caps how many NVENC
# sessions may be open at once; the cap varies by GPU/driver and isn't discoverable up front, so
# `default_jobs()`'s CPU-based guess can exceed it (observed: 24 logical cores -> 18 jobs, but
# this GPU only accepts ~10-13 concurrent h264_nvenc sessions; above that every open fails and
# the render is fatal). Rather than hard-code a number, back off at run time: on the first such
# failure, lower how many clips may hold an open NVENC encoder at once and retry that clip.
# ---------------------------------------------------------------------------------------------
_NVENC_CAPACITY_HINTS = ("could not open encoder", "generic error in an external library")


def _looks_like_nvenc_capacity_error(message: str) -> bool:
    low = message.lower()
    return "nvenc" in low and any(hint in low for hint in _NVENC_CAPACITY_HINTS)


class _NvencGate:
    """Admits at most `cap` concurrent holders; `shrink()` lowers `cap` (floor 1)."""

    def __init__(self, cap: int) -> None:
        self._cond = threading.Condition()
        self._cap = max(1, cap)
        self._active = 0

    @property
    def cap(self) -> int:
        with self._cond:
            return self._cap

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._cap:
                self._cond.wait()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def shrink(self) -> int | None:
        """Lower the cap by one; None if already at the floor (nothing left to try)."""
        with self._cond:
            if self._cap <= 1:
                return None
            self._cap -= 1
            self._cond.notify_all()
            return self._cap


def _render_with_nvenc_backoff(gate: "_NvencGate | None", segment_renderer: Callable[..., None],
                               tools: Tools, clip: ClipSpec, plan: Plan, enc: EncodeSettings,
                               dest: Path, threads: int, token: "_AnyEvent | None") -> None:
    if gate is None:
        segment_renderer(tools, clip, plan, enc, dest, threads, token)
        return
    while True:
        gate.acquire()
        try:
            segment_renderer(tools, clip, plan, enc, dest, threads, token)
            return
        except RenderError as e:
            if not _looks_like_nvenc_capacity_error(str(e)):
                raise
            new_cap = gate.shrink()
            if new_cap is None:
                raise
            log.warning("NVENC の同時エンコード数が上限を超えたため、同時実行数を %d に減らして再試行します", new_cap)
        finally:
            gate.release()


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
    drive = _choose_work_drive()
    base = drive / "mkaleido_tmp" if drive else None
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="mkaleido_", dir=base))
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
    backend = resolve_encoder_backend(tools, enc.backend)
    enc = dataclasses.replace(enc, backend=backend)
    frame_backend = _uses_frame_backend(plan)

    if runtime.jobs:
        jobs = runtime.jobs
    else:
        jobs = auto_jobs(_largest_source_pixels(plan, tools, inputs), plan, available_bytes(),
                         backend, frame_backend)
        if jobs < default_jobs(backend, frame_backend):
            log.info("並列数: %d(空きメモリに合わせて減らしました。--jobs で指定できます)", jobs)
    jobs = max(1, min(jobs, len(plan.clips)))
    threads = max(1, (os.cpu_count() or 1) // jobs)
    stop = threading.Event()
    token = _AnyEvent(cancel, stop)
    replaced: list[int] = []
    lock = threading.Lock()
    nvenc_gate = _NvencGate(jobs) if backend == "nvenc" else None

    work = _make_work_dir(runtime)
    log.debug("作業フォルダ: %s", work)
    try:
        seg_paths = [work / f"seg_{c.index + 1:05d}.mp4" for c in plan.clips]

        def task(clip: ClipSpec) -> None:
            dest = seg_paths[clip.index]
            try:
                _render_with_nvenc_backoff(nvenc_gate, segment_renderer, tools, clip, plan, enc,
                                           dest, threads, token)
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
                _render_with_nvenc_backoff(nvenc_gate, segment_renderer, tools, clip, plan, enc,
                                           dest, threads, token)

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
