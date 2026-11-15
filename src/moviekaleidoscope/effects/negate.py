"""negate: colour inversion (negative), blended by a moving amount."""

from __future__ import annotations

import cv2

from .base import Effect, ParamSpec, clamp, value_fn


class Negate(Effect):
    name = "negate"
    description = "色反転(ネガポジ)。反転の強さが時間変化する"
    backend = "frame"
    params = (ParamSpec("mix", 0.0, 1.0, doc="反転の強さ(0 で元の色、1 で完全な反転)"),)

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def frame_processor(self, params, ctx):
        mix_at = value_fn(self, params, "mix", ctx)

        def process(frame, t, n):
            mix = clamp(mix_at(t), 0.0, 1.0)
            if mix <= 0.0:
                return frame
            inv = cv2.bitwise_not(frame)
            return inv if mix >= 1.0 else cv2.addWeighted(frame, 1.0 - mix, inv, mix, 0)

        return process
