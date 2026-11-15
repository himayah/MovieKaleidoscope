"""zoom: scale about a moving centre (zooming out reflects the picture at the edges)."""

from __future__ import annotations

import cv2
import numpy as np

from .base import Effect, ParamSpec, clamp, value_fn


class Zoom(Effect):
    name = "zoom"
    description = "拡大縮小(中心も動く)。縮小したときは端を折り返して埋める"
    backend = "frame"
    params = (
        ParamSpec("scale", 0.5, 2.5, doc="倍率(1 未満は縮小)"),
        ParamSpec("cx", 0.3, 0.7, doc="拡大の中心 X(画面に対する割合)"),
        ParamSpec("cy", 0.3, 0.7, doc="拡大の中心 Y(画面に対する割合)"),
    )

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def frame_processor(self, params, ctx):
        s_at, x_at, y_at = (value_fn(self, params, k, ctx) for k in ("scale", "cx", "cy"))
        w, h = ctx.width, ctx.height

        def process(frame, t, n):
            s = clamp(s_at(t), 0.05, 20.0)
            cx, cy = clamp(x_at(t), 0.0, 1.0) * w, clamp(y_at(t), 0.0, 1.0) * h
            m = np.array([[s, 0.0, cx * (1 - s)], [0.0, s, cy * (1 - s)]])
            return cv2.warpAffine(frame, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)

        return process
