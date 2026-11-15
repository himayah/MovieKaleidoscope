"""strobe: flashes of black, white or inverted picture."""

from __future__ import annotations

import cv2
import numpy as np

from ..planner.motion import Motion
from .base import Effect, ParamSpec, phase_fn

MODES = ("black", "white", "invert")


class Strobe(Effect):
    name = "strobe"
    description = "ストロボ(黒・白・反転のフラッシュ)。点滅の速さが変わることもある。BPM があれば拍に合わせた点滅も"
    backend = "frame"
    params = (ParamSpec("rate", 2.0, 12.0, motions=("const", "ramp"), doc="点滅の回数(Hz)"),)
    BEAT_RATES = (0.5, 1.0, 2.0, 4.0)       # flashes per beat, when synced to the music

    def sample(self, rng, sctx, ranges, opts):
        if sctx.bpm and rng.random() < 0.5:
            out = {"unit": "beat", "rate": Motion.const(rng.choice(self.BEAT_RATES)).to_dict()}
        else:
            out = self.sample_params(rng, sctx, ranges)
            out["unit"] = "hz"
        out["mode"] = rng.choice(MODES)
        out["duty"] = round(rng.uniform(0.15, 0.5), 3)      # share of each cycle that is "on"
        return out

    def frame_processor(self, params, ctx):
        mode, duty = params["mode"], float(params["duty"])
        if mode not in MODES:
            raise ValueError(f"strobe の mode が不正です: {mode!r}")
        if not 0 < duty < 1:
            raise ValueError("strobe の duty は 0 より大きく 1 未満にしてください")
        unit = params.get("unit", "hz")
        if unit not in ("hz", "beat"):
            raise ValueError(f"strobe の unit が不正です: {unit!r}")
        cycles = phase_fn(self, params, "rate", ctx)
        if unit == "beat":
            if not ctx.bpm:
                raise ValueError("拍単位の strobe には BPM が必要です")
            per_beat = cycles
            cycles = lambda t: per_beat(t) * ctx.bpm / 60.0           # noqa: E731  flashes/beat -> cycles
        phase = cycles
        flat = {}

        def process(frame, t, n):
            if (phase(t) % 1.0) >= duty:
                return frame
            if mode == "invert":
                return cv2.bitwise_not(frame)
            if mode not in flat:
                flat[mode] = np.full(frame.shape, 255 if mode == "white" else 0, np.uint8)
            return flat[mode]

        return process
