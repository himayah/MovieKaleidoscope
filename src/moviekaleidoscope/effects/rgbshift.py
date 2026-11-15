"""rgbshift: displace the red and blue channels in opposite directions."""

from __future__ import annotations

import math

import cv2
import numpy as np

from .base import Effect, ParamSpec, RenderContext, value_fn


class RgbShift(Effect):
    name = "rgbshift"
    description = "R と B のチャンネルを逆方向にずらす(G は動かさない)。ずらす量が動く"
    backend = "ffmpeg"      # for a constant amount; a moving amount uses the frame backend
    params = (ParamSpec("amount", 2.0, 40.0, doc="ずらす量(ピクセル)"),)

    def sample(self, rng, sctx, ranges, opts):
        out = self.sample_params(rng, sctx, ranges)
        out["angle"] = round(rng.uniform(0, 360), 1)
        return out

    def backend_for(self, params) -> str:
        return "ffmpeg" if self.motion(params, "amount").kind == "const" else "frame"

    # ---- constant amount: ffmpeg's rgbashift (integer pixels) --------------------------------
    def post_filters(self, params, ctx: RenderContext) -> list[str]:
        if self.backend_for(params) != "ffmpeg":
            return []
        m = self.motion(params, "amount")
        a = math.radians(float(params["angle"]))
        rh, rv = round(m.base * math.cos(a)), round(m.base * math.sin(a))
        if max(abs(rh), abs(rv)) > 255:
            raise ValueError("rgbshift のずらし量が大きすぎます(最大 255)")
        # rgbashift works on RGB only. Convert explicitly (frames here are BT.709, limited range):
        # the conversions ffmpeg would insert pick their matrix from the frame tags, and those are
        # read differently by 4.4 and newer versions (a colour shift in one direction only).
        return ["scale=in_color_matrix=bt709:in_range=tv", "format=gbrp",
                f"rgbashift=rh={rh}:rv={rv}:bh={-rh}:bv={-rv}", "scale=out_color_matrix=bt709:out_range=tv"]

    # ---- moving amount: sub-pixel shifts in Python -----------------------------------------------
    def frame_processor(self, params, ctx):
        amount_at = value_fn(self, params, "amount", ctx)
        a = math.radians(float(params["angle"]))
        ux, uy = math.cos(a), math.sin(a)
        w, h = ctx.width, ctx.height

        def shift(ch: np.ndarray, dx: float, dy: float) -> np.ndarray:
            m = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]])
            return cv2.warpAffine(ch, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        def process(frame, t, n):
            amt = min(255.0, abs(amount_at(t))) * (1 if amount_at(t) >= 0 else -1)
            out = frame.copy()
            out[..., 0] = shift(frame[..., 0], amt * ux, amt * uy)      # red moves one way,
            out[..., 2] = shift(frame[..., 2], -amt * ux, -amt * uy)    # blue the other
            return out

        return process
