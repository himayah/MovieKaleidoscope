"""reverse: play the clip backwards."""

from __future__ import annotations

from .base import Effect, RenderContext, SampleContext


class Reverse(Effect):
    name = "reverse"
    description = "逆再生(断片をメモリに溜めるため、長い断片では使わない)"
    backend = "ffmpeg"
    options = {"max_sec": 10.0}
    needs_input_bound = True

    def available(self, sctx: SampleContext, opts) -> bool:
        return sctx.duration <= opts["max_sec"]

    def sample(self, rng, sctx, ranges, opts):
        return {}

    def post_filters(self, params, ctx: RenderContext) -> list[str]:
        # Cut to exactly the clip's frames first, so `reverse` sees an end of stream.
        return [f"trim=end_frame={ctx.frames}", "setpts=PTS-STARTPTS", "reverse", "setpts=PTS-STARTPTS"]
