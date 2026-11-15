"""kaleidoscope: fold the picture around a centre into N mirrored sectors."""

from __future__ import annotations

import math

import cv2
import numpy as np

from ._geom import pixel_grid
from .base import Effect, ParamSpec, clamp, value_fn


class Kaleidoscope(Effect):
    name = "kaleidoscope"
    description = "万華鏡(極座標で折り返す)。分割数は断片ごとに固定、回転と中心が動く"
    backend = "frame"
    params = (
        ParamSpec("angle", 0.0, 360.0, doc="回転角(度)。ramp なら回り続ける"),
        ParamSpec("cx", 0.4, 0.6, doc="中心 X(画面に対する割合)"),
        ParamSpec("cy", 0.4, 0.6, doc="中心 Y(画面に対する割合)"),
    )

    def sample(self, rng, sctx, ranges, opts):
        out = self.sample_params(rng, sctx, ranges)
        out["segments"] = rng.randint(3, 12)
        return out

    def frame_processor(self, params, ctx):
        n_seg = params["segments"]
        if not (isinstance(n_seg, int) and not isinstance(n_seg, bool) and 2 <= n_seg <= 64):
            raise ValueError(f"kaleidoscope の segments が不正です: {n_seg!r}")
        a_at, x_at, y_at = (value_fn(self, params, k, ctx) for k in ("angle", "cx", "cy"))
        w, h = ctx.width, ctx.height
        sector = 2 * math.pi / n_seg
        polar: dict = {}      # (cx, cy) -> (radius, theta): reused while the centre stands still

        def process(frame, t, n):
            cx, cy = clamp(x_at(t), 0.0, 1.0) * w, clamp(y_at(t), 0.0, 1.0) * h
            if polar.get("key") != (cx, cy):
                gx, gy = pixel_grid(w, h)
                dx, dy = gx - np.float32(cx), gy - np.float32(cy)
                polar.update(key=(cx, cy), r=np.hypot(dx, dy), th=np.arctan2(dy, dx))
            a = np.mod(polar["th"], sector)
            a = np.where(a > sector / 2, sector - a, a) + math.radians(a_at(t))   # fold, then rotate
            map_x = (cx + polar["r"] * np.cos(a)).astype(np.float32)
            map_y = (cy + polar["r"] * np.sin(a)).astype(np.float32)
            return cv2.remap(frame, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)

        return process
