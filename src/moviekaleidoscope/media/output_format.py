"""Output resolution / FPS resolution (SPEC §3.3)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from ..config import VideoConfig
from .probe import VideoInfo

FALLBACK_FPS = Fraction(30)
MAX_DEFAULT_LONG, MAX_DEFAULT_SHORT = 1920, 1080

# Rates a default (not user-given) FPS is snapped to. VFR sources report an
# avg_frame_rate such as 3120/179 (~17.43); phones often report 2997003/100000.
STANDARD_FPS = (
    Fraction(15), Fraction(24000, 1001), Fraction(24), Fraction(25),
    Fraction(30000, 1001), Fraction(30), Fraction(48), Fraction(50),
    Fraction(60000, 1001), Fraction(60), Fraction(100), Fraction(120),
)


def snap_fps(fps: Fraction) -> Fraction:
    """Nearest standard rate by ratio (log distance); ties go to the lower rate."""
    if fps in STANDARD_FPS:
        return fps
    return min(STANDARD_FPS, key=lambda s: abs(math.log(fps / s)))


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
        # Not chosen by the user: no larger than 1080p (long side 1920, short side 1080; either
        # orientation). A 4K first input made a 4K video: 1.7 GB for 3.4 minutes and 17 GB of
        # memory while rendering (2026-11-16). --width/--height still give 4K when asked for.
        scale = min(1.0, MAX_DEFAULT_LONG / max(w, h), MAX_DEFAULT_SHORT / min(w, h))
        w, h = w * scale, h * scale

    if video.fps is not None:
        fps = video.fps
    else:
        fps = snap_fps(first.fps) if 1 <= first.fps <= 120 else FALLBACK_FPS
    return OutputFormat(_even(w), _even(h), fps)
