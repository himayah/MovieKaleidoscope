"""Frame backend: decode (ffmpeg) -> Python frame effects -> encode (ffmpeg), via raw pipes."""

from __future__ import annotations

import queue
import subprocess
import tempfile
import threading
from typing import Sequence

import numpy as np

from ..errors import Cancelled, RenderError
from ..media.tools import Tools
from ..planner.plan import ClipSpec, Plan
from ..util.process import CHILD_FLAGS, CancelToken, terminate
from .ffmpeg import ClipChain, EncodeSettings, decode_command, encode_command

# How many decoded frames the reader thread may read ahead of the Python/encode side. This lets
# ffmpeg-decode, the Python effect and ffmpeg-encode overlap instead of running in lockstep; it
# also adds this many frames' worth of RGB24 memory per job (see pipeline.job_memory_estimate()).
FRAME_QUEUE_MAXSIZE = 3


def _tail(f) -> str:
    f.seek(0)
    lines = [ln for ln in f.read().decode("utf-8", "replace").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-6:]) or "(ffmpeg からのメッセージなし)"


def _read_frame(stream, buf: np.ndarray) -> bool:
    """Fill `buf` completely from the pipe; False if the stream ended first."""
    view = memoryview(buf).cast("B")
    got = 0
    while got < len(view):
        n = stream.readinto(view[got:])
        if not n:
            return False
        got += n
    return True


def render_frame_segment(tools: Tools, clip: ClipSpec, plan: Plan, enc: EncodeSettings, dest,
                         threads: int, chain: ClipChain, cancel: CancelToken | None) -> None:
    """Render one clip through the Python frame processors into `dest`.

    A background thread reads decoded frames ahead into a bounded queue (`FRAME_QUEUE_MAXSIZE`)
    so ffmpeg-decode overlaps with the Python effect and the ffmpeg-encode write, instead of the
    three running in strict lockstep. Encoding still happens on this thread, synchronously."""
    out = plan.output
    w, h, fps = out.width, out.height, float(out.fps)
    frame_shape = (h, w, 3)
    processors: Sequence = chain.processors
    with tempfile.TemporaryFile() as dec_err, tempfile.TemporaryFile() as enc_err:
        dec = subprocess.Popen(decode_command(tools, clip, chain, out.fps), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=dec_err, bufsize=w * h * 3 * 2,
                               creationflags=CHILD_FLAGS)
        enc_p = subprocess.Popen(encode_command(tools, plan, enc, dest, threads), stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=enc_err, creationflags=CHILD_FLAGS)
        q: "queue.Queue[np.ndarray | None]" = queue.Queue(maxsize=FRAME_QUEUE_MAXSIZE)
        reader_error: list[Exception] = []

        def reader() -> None:
            try:
                while True:
                    buf = np.empty(frame_shape, np.uint8)
                    if not _read_frame(dec.stdout, buf):
                        q.put(None)
                        return
                    q.put(buf)
            except (OSError, ValueError):
                # dec.stdout was closed from the main thread during shutdown: a clean stop.
                q.put(None)
            except Exception as e:      # an unexpected failure (e.g. allocation): surface it
                reader_error.append(e)
                q.put(None)

        reader_thread = threading.Thread(target=reader, daemon=True, name="mkaleido-frame-reader")
        reader_thread.start()
        try:
            for n in range(clip.frames):
                if cancel is not None and cancel.is_set():
                    raise Cancelled("中断しました")
                frame = q.get()
                if frame is None:
                    if reader_error:
                        raise reader_error[0]
                    raise RenderError(f"クリップ {clip.index}: 素材から {clip.frames} フレームを読めませんでした"
                                      f"({n} フレームで終了、素材 {clip.source.path}):\n{_tail(dec_err)}")
                t = n / fps
                for process in processors:
                    try:
                        frame = process(frame, t, n)
                    except Exception as e:      # a bug or bad parameter in an effect
                        raise RenderError(f"クリップ {clip.index}: エフェクトの処理に失敗しました"
                                          f"(フレーム {n}、{type(e).__name__}: {e})") from e
                if frame.shape != frame_shape or frame.dtype != np.uint8:
                    raise RenderError(f"クリップ {clip.index}: エフェクトが不正な形式のフレームを返しました"
                                      f"({frame.shape}, {frame.dtype})")
                try:
                    enc_p.stdin.write(np.ascontiguousarray(frame).data)
                except (BrokenPipeError, OSError):
                    enc_p.wait()
                    raise RenderError(f"クリップ {clip.index}: エンコーダが異常終了しました:\n{_tail(enc_err)}") from None
            enc_p.stdin.close()
            if enc_p.wait() != 0:
                raise RenderError(f"クリップ {clip.index}: エンコードに失敗しました:\n{_tail(enc_err)}")
            if dec.wait() != 0:
                raise RenderError(f"クリップ {clip.index}: デコードに失敗しました:\n{_tail(dec_err)}")
        finally:
            # Close the encoder's stdin first: a well-behaved encoder exits on EOF without
            # waiting to be killed. Then force-kill the decoder -- this also breaks the reader
            # thread's pending read (the pipe's write end dies with the process), so the reader
            # notices EOF and returns on its own instead of us racing a close against its read.
            if enc_p.stdin is not None:
                try:
                    enc_p.stdin.close()
                except OSError:
                    pass
            terminate(dec)
            while True:     # release a reader thread blocked on a full queue (nothing draining it)
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            reader_thread.join(timeout=5)
            if dec.stdout is not None:
                try:
                    dec.stdout.close()
                except OSError:
                    pass
            terminate(enc_p)
