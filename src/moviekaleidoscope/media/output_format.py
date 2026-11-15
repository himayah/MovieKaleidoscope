"""Output resolution / FPS resolution (SPEC §3.3)."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from ..config import VideoConfig
from .probe import VideoInfo

FALLBACK_FPS = Fraction(30)


@dataclass(frozen=True)
class OutputFormat:
    width: int
    height: int
    fps: Fraction


def _even(n: float) -> int:
    n = int(round(n))
    return max(2, n - n % 2)


def resolve_output_format(video: VideoConfig, first: VideoInfo) -> OutputFormat:
    """Defaults come from the first valid input; width/height/fps can override."""
    if video.width and video.height:
        w, h = video.width, video.height
    elif video.width:
        w, h = video.width, video.width * first.height / first.width
    elif video.height:
        w, h = video.height * first.width / first.height, video.height
    else:
        w, h = first.width, first.height

    if video.fps is not None:
        fps = video.fps
    else:
        fps = first.fps if 1 <= first.fps <= 120 else FALLBACK_FPS
    return OutputFormat(_even(w), _even(h), fps)
