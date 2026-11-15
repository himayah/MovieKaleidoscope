"""solarize: invert the tones above a moving threshold."""

from __future__ import annotations

import cv2
import numpy as np

from .base import Effect, ParamSpec, clamp, value_fn

CHANNEL_SETS = ("rgb", "r", "g", "b", "rg", "rb", "gb")


class Solarize(Effect):
    name = "solarize"
    description = "しきい値以上の明るさを反転する(ソラライズ)。しきい値が動き、一部のチャンネルだけに効くこともある"
    backend = "frame"
    params = (ParamSpec("threshold", 60.0, 200.0, doc="しきい値(0〜255)"),)

    def sample(self, rng, sctx, ranges, opts):
        out = self.sample_params(rng, sctx, ranges)
        out["channels"] = rng.choice(CHANNEL_SETS)
        return out

    def frame_processor(self, params, ctx):
        channels = params["channels"]
        if channels not in CHANNEL_SETS:
            raise ValueError(f"solarize の channels が不正です: {channels!r}")
        thr_at = value_fn(self, params, "threshold", ctx)
        base = np.arange(256, dtype=np.uint8)
        cache: dict[int, np.ndarray] = {}

        def lut_for(thr: int) -> np.ndarray:
            if thr not in cache:
                inv = np.where(base >= thr, 255 - base, base).astype(np.uint8)
                lut = np.empty((256, 1, 3), np.uint8)
                for i, ch in enumerate("rgb"):
                    lut[:, 0, i] = inv if ch in channels else base
                cache[thr] = lut
            return cache[thr]

        def process(frame, t, n):
            return cv2.LUT(frame, lut_for(int(clamp(round(thr_at(t)), 0, 255))))

        return process
