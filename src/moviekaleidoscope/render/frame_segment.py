"""Frame backend: decode (ffmpeg) -> Python frame effects -> encode (ffmpeg), via raw pipes."""

from __future__ import annotations

import subprocess
import tempfile
from typing import Sequence

import numpy as np

from ..errors import Cancelled, RenderError
from ..media.tools import Tools
from ..planner.plan import ClipSpec, Plan
from ..util.process import CHILD_FLAGS, CancelToken, terminate
from .ffmpeg import ClipChain, EncodeSettings, decode_command, encode_command


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
    """Render one clip through the Python frame processors into `dest`."""
    out = plan.output
    w, h, fps = out.width, out.height, float(out.fps)
    processors: Sequence = chain.processors
    with tempfile.TemporaryFile() as dec_err, tempfile.TemporaryFile() as enc_err:
        dec = subprocess.Popen(decode_command(tools, clip, chain, out.fps), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=dec_err, bufsize=w * h * 3 * 2,
                               creationflags=CHILD_FLAGS)
        enc_p = subprocess.Popen(encode_command(tools, plan, enc, dest, threads), stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=enc_err, creationflags=CHILD_FLAGS)
        try:
            for n in range(clip.frames):
                if cancel is not None and cancel.is_set():
                    raise Cancelled("中断しました")
                frame = np.empty((h, w, 3), np.uint8)
                if not _read_frame(dec.stdout, frame):
                    terminate(dec)
                    raise RenderError(f"クリップ {clip.index}: 素材から {clip.frames} フレームを読めませんでした"
                                      f"({n} フレームで終了、素材 {clip.source.path}):\n{_tail(dec_err)}")
                t = n / fps
                for process in processors:
                    try:
                        frame = process(frame, t, n)
                    except Exception as e:      # a bug or bad parameter in an effect
                        raise RenderError(f"クリップ {clip.index}: エフェクトの処理に失敗しました"
                                          f"(フレーム {n}、{type(e).__name__}: {e})") from e
                if frame.shape != (h, w, 3) or frame.dtype != np.uint8:
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
            # Close our ends of the pipes FIRST. A decoder blocked writing into a full pipe does
            # not react to SIGTERM until the write returns; with the reader gone it gets EPIPE and
            # exits at once. (Terminating first cost 5-10 s per failed or cancelled clip.)
            for p in (dec, enc_p):
                for pipe in (p.stdin, p.stdout):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
            for p in (dec, enc_p):
                terminate(p)
