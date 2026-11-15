"""mirror: reflect one side of the picture onto the other."""

from __future__ import annotations

import numpy as np

from ._geom import reflect_index
from .base import Effect, ParamSpec, clamp, value_fn

MODES = ("lr", "rl", "tb", "bt", "quad")   # lr = left side is mirrored onto the right


class Mirror(Effect):
    name = "mirror"
    description = "片側を反対側へ鏡写しにする(左→右、右→左、上→下、下→上、四分割)。軸の位置が動く"
    backend = "frame"
    params = (ParamSpec("pos", 0.3, 0.7, doc="鏡の軸の位置(画面に対する割合)"),)

    def sample(self, rng, sctx, ranges, opts):
        out = self.sample_params(rng, sctx, ranges)
        out["mode"] = rng.choice(MODES)
        return out

    def frame_processor(self, params, ctx):
        mode = params["mode"]
        if mode not in MODES:
            raise ValueError(f"mirror の mode が不正です: {mode!r}")
        pos_at = value_fn(self, params, "pos", ctx)
        w, h = ctx.width, ctx.height

        def fold(out, axis, size, p, keep_low):
            """Mirror the kept side of the axis (at p*size) onto the other side, in place."""
            a = max(1, min(size - 1, int(clamp(p, 0.05, 0.95) * size)))
            if keep_low:       # keep [0, a); x in [a, size) shows pixel 2a-1-x, reflected into [0, a)
                idx = reflect_index(2 * a - 1 - np.arange(a, size), a)
                dst = slice(a, size)
            else:              # keep [a, size); x in [0, a) shows pixel 2a-1-x, reflected into [a, size)
                idx = a + reflect_index(a - 1 - np.arange(0, a), size - a)
                dst = slice(0, a)
            if axis == 1:
                out[:, dst] = out[:, idx]
            else:
                out[dst] = out[idx]

        def process(frame, t, n):
            p = pos_at(t)
            out = frame.copy()
            if mode in ("lr", "quad"):
                fold(out, 1, w, p, True)
            if mode == "rl":
                fold(out, 1, w, p, False)
            if mode in ("tb", "quad"):
                fold(out, 0, h, p, True)
            if mode == "bt":
                fold(out, 0, h, p, False)
            return out

        return process
