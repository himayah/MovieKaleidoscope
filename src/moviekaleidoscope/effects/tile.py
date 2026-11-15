"""tile: repeat the picture over the plane, then scroll and/or rotate the view."""

from __future__ import annotations

import math

import cv2
import numpy as np

from ._geom import pixel_grid, wrap_coords
from .base import Effect, ParamSpec, phase_fn, value_fn

BEAT_COUNTS = (1, 2, 4, 8)
TRANSFORMS = ("none", "scroll", "rotate", "scroll+rotate")


def _beat_count(n, ctx) -> int:
    if not ctx.bpm:
        raise ValueError("拍単位の tile には BPM が必要です")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"tile の拍数が不正です: {n!r}")
    return n
WRAPS = ("repeat", "mirror")


class Tile(Effect):
    name = "tile"
    description = "画像を縦横に繋いで(タイリング)、スクロール・回転させる。繋ぎ方は繰り返しか鏡像"
    backend = "frame"
    params = (
        ParamSpec("speed", 0.2, 1.5, motions=("const", "ramp"), doc="スクロール速度(タイル/秒)"),
        ParamSpec("angle", 0.0, 360.0, doc="回転角(度)。ramp なら回り続ける"),
    )

    def sample(self, rng, sctx, ranges, opts):
        transform = rng.choice(TRANSFORMS)
        out = {"transform": transform, "wrap": rng.choice(WRAPS), "tile_n": rng.choice([2, 3, 4, 5, 6])}
        synced = sctx.bpm is not None
        if "scroll" in transform:
            out["scroll_angle"] = round(rng.uniform(0, 360), 1)
            if synced and rng.random() < 0.5:
                out["scroll_beats"] = rng.choice(BEAT_COUNTS)      # exactly one tile every N beats: loops seamlessly
            else:
                out["speed"] = self.sample_params(rng, sctx, ranges, ("speed",))["speed"]
        if "rotate" in transform:
            if synced and rng.random() < 0.5:
                out["rotate_beats"] = rng.choice((1, 2, 4))        # a quarter turn every N beats
                out["rotate_dir"] = rng.choice((-1, 1))
            else:
                out["angle"] = self.sample_params(rng, sctx, ranges, ("angle",))["angle"]
        return out

    def frame_processor(self, params, ctx):
        transform, wrap, n_tiles = params["transform"], params["wrap"], float(params["tile_n"])
        if transform not in TRANSFORMS or wrap not in WRAPS or not 1 <= n_tiles <= 16:
            raise ValueError(f"tile のパラメータが不正です: {transform!r}, {wrap!r}, {n_tiles!r}")
        scrolling, rotating = "scroll" in transform, "rotate" in transform
        dist = angle_at = None
        if scrolling:
            if "scroll_beats" in params:                                           # one tile every N beats
                n_beats = _beat_count(params["scroll_beats"], ctx)
                dist = lambda t: t * ctx.bpm / 60.0 / n_beats                      # noqa: E731
            else:
                dist = phase_fn(self, params, "speed", ctx)                        # tiles travelled
        phi = math.radians(float(params["scroll_angle"])) if scrolling else 0.0
        if rotating:
            if "rotate_beats" in params:                                           # 90 degrees every N beats
                n_beats, sign = _beat_count(params["rotate_beats"], ctx), params["rotate_dir"]
                if sign not in (-1, 1):
                    raise ValueError(f"tile の rotate_dir が不正です: {sign!r}")
                angle_at = lambda t: sign * 90.0 * math.floor(t * ctx.bpm / 60.0 / n_beats + 1e-9)   # noqa: E731
            else:
                angle_at = value_fn(self, params, "angle", ctx)
        mirror = wrap == "mirror"
        w, h = ctx.width, ctx.height
        static: list = []        # the map never changes when nothing moves

        def build(t: float):
            gx, gy = pixel_grid(w, h)
            x, y = gx - np.float32(w / 2), gy - np.float32(h / 2)
            if rotating:
                a = math.radians(angle_at(t))
                c, s = math.cos(a), math.sin(a)
                x, y = c * x - s * y, s * x + c * y
            u, v = x * n_tiles + w / 2, y * n_tiles + h / 2
            if scrolling:
                d = dist(t)
                u, v = u + d * math.cos(phi) * w, v + d * math.sin(phi) * h
            return (wrap_coords(u, w, mirror).astype(np.float32), wrap_coords(v, h, mirror).astype(np.float32))

        def process(frame, t, n):
            if not (scrolling or rotating):
                if not static:
                    static.append(build(0.0))
                maps = static[0]
            else:
                maps = build(t)
            return cv2.remap(frame, maps[0], maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        return process
