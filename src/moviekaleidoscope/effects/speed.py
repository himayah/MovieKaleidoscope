"""speed: fast-forward / slow motion, optionally accelerating or decelerating."""

from __future__ import annotations

from .base import Effect, ParamSpec, RenderContext, q
from ..planner.motion import num


class Speed(Effect):
    name = "speed"
    description = "早送り・スロー(0.25〜4 倍)。加速・減速することもある"
    backend = "ffmpeg"
    params = (ParamSpec("rate", 0.25, 4.0, motions=("const", "ramp"), log=True,
                        doc="再生速度の倍率。ramp は出力時間に対して線形に変化"),)

    def sample(self, rng, sctx, ranges, opts):
        return self.sample_params(rng, sctx, ranges)

    def source_span(self, params, duration: float) -> float:
        # const: duration*rate. ramp (linear in output time): duration*(start+end)/2.
        return duration * self.motion(params, "rate").mean()

    def pre_filters(self, params, ctx: RenderContext) -> list[str]:
        m = self.motion(params, "rate")
        a = m.base
        b = m.end if m.kind == "ramp" else m.base
        if min(a, b) <= 0:
            raise ValueError("rate は正の値にしてください")
        if abs(b - a) < 1e-3:
            return [f"setpts=PTS/{num((a + b) / 2)}"]
        # Source time u(T) = a*T + (b-a)*T^2/(2D) for output time T; setpts needs T(u).
        d = num(ctx.duration)
        expr = (f"{d}*(sqrt(max(0,{num(a)}*{num(a)}+2*{num(b - a)}*PTS*TB/{d}))-{num(a)})"
                f"/{num(b - a)}/TB")
        return [f"setpts={q(expr)}"]
