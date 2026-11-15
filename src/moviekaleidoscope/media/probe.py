"""ffprobe wrapper: reads just enough metadata, never decodes frames."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..util.process import run_capture
from .tools import Tools


class ProbeError(Exception):
    """The file cannot be used as a video input. str(e) is a human-readable reason."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int    # display size (rotation applied)
    height: int
    fps: Fraction
    duration: float
    matrix: str = "bt709"   # YCbCr matrix of the source (see matrix_of)


def _parse_rate(s: Any) -> Fraction | None:
    try:
        f = Fraction(str(s))
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def _parse_seconds(s: Any) -> float | None:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _rotation(stream: dict) -> int:
    rot: Any = (stream.get("tags") or {}).get("rotate")
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    try:
        return int(round(float(rot))) % 360
    except (TypeError, ValueError):
        return 0


# ffprobe `color_space` -> name accepted by ffmpeg's scale filter (in_color_matrix)
_MATRICES = {
    "bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601", "bt470m": "bt470",
    "fcc": "fcc", "smpte240m": "smpte240m", "bt2020nc": "bt2020", "bt2020c": "bt2020",
}


def matrix_of(stream: dict, height: int) -> str:
    """The source's YCbCr matrix: its tag if it has one, else the usual player convention
    (HD = BT.709, SD = BT.601)."""
    tagged = _MATRICES.get(str(stream.get("color_space", "")).lower())
    if tagged:
        return tagged
    return "bt709" if height >= 720 else "bt601"


def probe_video(path: Path, tools: Tools, timeout: float = 30.0) -> VideoInfo:
    """Return VideoInfo or raise ProbeError with the reason."""
    args = [tools.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-i", str(path)]
    try:
        code, out, err = run_capture(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe がタイムアウトしました") from None
    except OSError as e:
        raise ProbeError(f"ffprobe を実行できません: {e}") from None
    if code != 0:
        first = (err.strip().splitlines() or ["不明なエラー"])[0]
        raise ProbeError(f"動画として読めません({first})")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ProbeError("ffprobe の出力を解釈できません") from None

    stream = next(
        (s for s in data.get("streams", [])
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if stream is None:
        raise ProbeError("映像ストリームがありません")

    w, h = stream.get("width"), stream.get("height")
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        raise ProbeError("解像度を取得できません")
    coded_height = h
    if _rotation(stream) in (90, 270):
        w, h = h, w

    fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
    if fps is None:
        raise ProbeError("フレームレートを取得できません")

    duration = _parse_seconds(stream.get("duration")) or _parse_seconds((data.get("format") or {}).get("duration"))
    if duration is None:
        raise ProbeError("長さを取得できません")

    return VideoInfo(path=path, width=w, height=h, fps=fps, duration=duration,
                     matrix=matrix_of(stream, coded_height))


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    duration: float


def probe_audio(path: Path, tools: Tools, timeout: float = 30.0) -> AudioInfo:
    """Return AudioInfo or raise ProbeError. Needs a real audio stream."""
    args = [tools.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-i", str(path)]
    try:
        code, out, err = run_capture(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe がタイムアウトしました") from None
    except OSError as e:
        raise ProbeError(f"ffprobe を実行できません: {e}") from None
    if code != 0:
        raise ProbeError(f"音声として読めません({(err.strip().splitlines() or ['不明なエラー'])[0]})")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ProbeError("ffprobe の出力を解釈できません") from None
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise ProbeError("音声ストリームがありません")
    duration = _parse_seconds(stream.get("duration")) or _parse_seconds((data.get("format") or {}).get("duration"))
    if duration is None:
        raise ProbeError("長さを取得できません")
    return AudioInfo(path, duration)
