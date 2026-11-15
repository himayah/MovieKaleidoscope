"""posterize: reduce the number of tone levels."""

from __future__ import annotations

import cv2
import numpy as np

from .base import Effect, ParamSpec, clamp, value_fn


class Posterize(Effect):
    name = "posterize"
    description = "階調を減らす(ポスタライズ)。階調数が時間変化する"
    backend = "frame"
    params = (ParamSpec("levels", 2.0, 8.0, doc="1 チャンネルあたりの階調数"),)

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def frame_processor(self, params, ctx):
        levels_at = value_fn(self, params, "levels", ctx)
        i = np.arange(256)
        cache: dict[int, np.ndarray] = {}

        def process(frame, t, n):
            lv = int(clamp(round(levels_at(t)), 2, 64))
            if lv not in cache:
                cache[lv] = (np.floor(i * lv / 256) * 255 / (lv - 1)).astype(np.uint8)
            return cv2.LUT(frame, cache[lv])

        return process
