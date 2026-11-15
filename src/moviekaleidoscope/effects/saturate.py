"""saturate: saturation boost."""

from __future__ import annotations

from .base import Effect, ParamSpec, RenderContext, q


class Saturate(Effect):
    name = "saturate"
    description = "彩度を強く上げる"
    backend = "ffmpeg"
    params = (ParamSpec("gain", 1.5, 4.0, doc="彩度の倍率"),)

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def post_filters(self, params, ctx: RenderContext) -> list[str]:
        m = self.motion(params, "gain")
        return [f"hue=s={q(m.expr(ctx.duration, ctx.bpm))}"]
