"""hue: rotate the hue."""

from __future__ import annotations

from .base import Effect, ParamSpec, RenderContext, q


class Hue(Effect):
    name = "hue"
    description = "色相を回転させる(回転の向き・速さ・揺れはランダム)"
    backend = "ffmpeg"
    params = (ParamSpec("angle", 0.0, 720.0, doc="色相角(度)。ramp なら回転、sine なら揺れ"),)

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def post_filters(self, params, ctx: RenderContext) -> list[str]:
        m = self.motion(params, "angle")
        return [f"hue=h={q(m.expr(ctx.duration, ctx.bpm))}"]
