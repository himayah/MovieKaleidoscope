"""negate: colour inversion (negative), blended by a moving amount."""

from __future__ import annotations

import math

import cv2

from .base import Effect, ParamSpec, clamp, value_fn


# A blend keeps |1 - 2*mix| of the picture's contrast: at mix 0.5 every pixel is (x + 255-x)/2,
# a flat grey frame (seen on real footage, C3 2026-11-16). The used mix stays at least this far
# from 0.5, and crossing the middle is an instant switch.
MIN_CONTRAST = 0.6


def avoid_grey(mix: float) -> float:
    """Map 0..1 onto [0, 0.2] and [0.8, 1]: same order and ends, nothing in the washed-out middle."""
    d = mix - 0.5
    if d == 0.0:
        return 1.0
    gap = MIN_CONTRAST / 2                       # 0.3: distance of the nearest used value from 0.5
    return 0.5 + math.copysign(gap + (0.5 - gap) * abs(2 * d), d)


class Negate(Effect):
    name = "negate"
    description = "色反転(ネガポジ)。反転の強さが時間変化する"
    backend = "frame"
    params = (ParamSpec("mix", 0.0, 1.0, doc="反転の強さ(0 で元の色、1 で完全な反転)。灰色になる中間(0.2〜0.8)は使わず、瞬時に切り替える"),)

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def frame_processor(self, params, ctx):
        mix_at = value_fn(self, params, "mix", ctx)

        def process(frame, t, n):
            mix = avoid_grey(clamp(mix_at(t), 0.0, 1.0))
            if mix <= 0.0:
                return frame
            inv = cv2.bitwise_not(frame)
            return inv if mix >= 1.0 else cv2.addWeighted(frame, 1.0 - mix, inv, mix, 0)

        return process
