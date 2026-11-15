"""feedback: the previous output is zoomed/rotated, dimmed and kept as a trail."""

from __future__ import annotations

import cv2
import numpy as np

from .base import Effect, ParamSpec, clamp, value_fn


class Feedback(Effect):
    name = "feedback"
    description = "フィードバック(残像)。直前の出力を拡大・回転・減衰させながら重ねる"
    backend = "frame"
    params = (
        ParamSpec("decay", 0.55, 0.95, doc="残像の残りやすさ(1 に近いほど長く残る)"),
        ParamSpec("zoom", 0.98, 1.06, doc="1 フレームごとの拡大率"),
        ParamSpec("rot", -4.0, 4.0, doc="1 フレームごとの回転(度)"),
    )

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def frame_processor(self, params, ctx):
        d_at, z_at, r_at = (value_fn(self, params, k, ctx) for k in ("decay", "zoom", "rot"))
        w, h = ctx.width, ctx.height
        prev: list[np.ndarray] = []      # state of this clip only

        def process(frame, t, n):
            if not prev:
                out = frame
            else:
                m = cv2.getRotationMatrix2D((w / 2, h / 2), r_at(t), clamp(z_at(t), 0.5, 2.0))
                trail = cv2.warpAffine(prev[0], m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
                out = np.maximum(frame, cv2.convertScaleAbs(trail, alpha=clamp(d_at(t), 0.0, 1.0)))
            prev[:] = [out]
            return out

        return process
