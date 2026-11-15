"""flip: geometric flip (horizontal / vertical / both), optionally alternating in time."""

from __future__ import annotations

from .base import Effect, RenderContext, SampleContext, q, sample_period
from ..planner.motion import num

AXES = {"h": ("hflip",), "v": ("vflip",), "hv": ("hflip", "vflip")}


class Flip(Effect):
    name = "flip"
    description = "上下・左右の反転(一定の間隔で反転と元を交互に切り替えることもある)"
    backend = "ffmpeg"

    def sample(self, rng, sctx: SampleContext, ranges, opts):
        alternate = None
        if rng.random() < 0.5:
            period, unit = sample_period(rng, sctx, (1, 2, 4), (0.3, 1.5))
            alternate = {"period": period, "unit": unit}
        return {"axis": rng.choice(sorted(AXES)), "alternate": alternate}

    def post_filters(self, params, ctx: RenderContext) -> list[str]:
        filters = AXES[params["axis"]]
        alt = params.get("alternate")
        if alt is None:
            return list(filters)
        unit = alt["unit"]
        if unit not in ("sec", "beat"):
            raise ValueError(f"alternate の単位が不正です: {unit!r}")
        p = float(alt["period"])
        if p <= 0:
            raise ValueError("alternate の period は正の値にしてください")
        if unit == "beat":
            if not ctx.bpm:
                raise ValueError("拍単位の alternate には BPM が必要です")
            p = p * 60.0 / ctx.bpm
        # flipped during the first period, normal during the second, and so on
        enable = q(f"lt(mod(t,2*{num(p)}),{num(p)})")
        return [f"{f}=enable={enable}" for f in filters]
