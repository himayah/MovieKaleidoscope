"""ffmpeg command construction (pure functions; nothing is executed here)."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..effects import registry
from ..effects.base import FrameProcessor, RenderContext
from ..media.tools import Tools
from ..planner.plan import ClipSpec, Plan


@dataclass(frozen=True)
class EncodeSettings:
    crf: int = 18
    preset: str = "medium"


def _fps_arg(fps) -> str:
    return f"{fps.numerator}/{fps.denominator}"


def render_context(clip: ClipSpec, plan: Plan) -> RenderContext:
    out = plan.output
    return RenderContext(
        duration=float(Fraction(clip.frames) / out.fps), frames=clip.frames, fps=out.fps,
        width=out.width, height=out.height, bpm=plan.audio.bpm if plan.audio else None,
    )


@dataclass
class ClipChain:
    """How one clip is rendered: an ffmpeg filter chain, plus Python frame processors (if any)."""

    filters: list[str]
    input_bound: bool                      # a filter reads to end-of-stream (reverse)
    processors: list[FrameProcessor]       # applied in Python after decoding, in order

    @property
    def uses_frame_backend(self) -> bool:
        return bool(self.processors)


def build_chain(clip: ClipSpec, plan: Plan) -> ClipChain:
    """Filter chain for one clip: normalise to the output format, then apply its effects.

    Order:  setpts(0) -> time effects (speed) -> fps -> scale/crop -> ffmpeg image effects
            -> format.  Frame-backend effects run afterwards, in Python, on RGB frames
    (the chain then ends in rgb24 instead of yuv420p).
    """
    out = plan.output
    ctx = render_context(clip, plan)
    pre: list[str] = []
    post: list[str] = []
    processors: list[FrameProcessor] = []
    bound = False
    for spec in clip.effects:
        effect = registry.get(spec.type)
        if effect.backend_for(spec.params) == "frame":
            processors.append(effect.frame_processor(spec.params, ctx))
        else:
            pre += effect.pre_filters(spec.params, ctx)
            post += effect.post_filters(spec.params, ctx)
            bound = bound or effect.needs_input_bound
    matrix = clip.source.matrix
    to_709: list[str] = []
    if not processors and matrix != "bt709":
        # YUV -> YUV matrix changes are not done by swscale, so go through RGB (inside ffmpeg).
        to_709 = [f"scale=in_color_matrix={matrix}", "format=gbrp", "scale=out_color_matrix=bt709:out_range=tv"]
    to_rgb = [f"scale=in_color_matrix={matrix}", "format=rgb24"] if processors else []
    filters = [
        # First frame at exactly t=0: after a seek the first frame sits slightly after 0,
        # and `fps` would round it to the next slot, leaving a gap at the start of every
        # segment that stream-copy concat turns into a hitch at every cut.
        "setpts=PTS-STARTPTS",
        *pre,
        f"fps={_fps_arg(out.fps)}",
        # The matrix is explicit on both sides: ffmpeg 7.1+ (not 6.1) negotiates colour
        # properties across the graph, so an untagged source would otherwise be read as BT.601
        # and converted to the encoder's BT.709 tag (a visible shift; 4.4 never did this).
        f"scale={out.width}:{out.height}:force_original_aspect_ratio=increase"
        f":in_color_matrix={matrix}:out_color_matrix={matrix}:out_range=tv",
        f"crop={out.width}:{out.height}",
        "setsar=1",
        *to_709,
        *post,
        *(to_rgb or ["format=yuv420p"]),
    ]
    return ClipChain(filters, bound, processors)


def input_args(clip: ClipSpec, chain: ClipChain) -> list[str]:
    src = clip.source
    args = ["-stream_loop", "-1"] if src.loop else ["-ss", f"{src.in_point:.3f}"]
    if chain.input_bound:
        # A filter that reads to end-of-stream (reverse) must not be fed the rest of the file.
        args += ["-t", f"{src.span + 0.5:.3f}"]
    return [*args, "-i", str(src.path)]


# Tags on the frames themselves. ffmpeg 7.1+ (not 6.1) takes the stream's colour tags from
# the frames and ignores -color_primaries / -color_trc when the frames say "unspecified"; 4.4 used
# the encoder options. Setting both works everywhere.
TAG_BT709 = "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"


def encoder_args(enc: EncodeSettings) -> list[str]:
    # Identical on every segment so that the final concat can stream-copy.
    # `-bf 0` (no B-frames) is a safeguard, not a fix for a seen failure: without B-frames
    # a segment has no decode-delay edit list, so stream-copy concat timing does not depend
    # on how a given ffmpeg version writes those (verified gap-free on 4.4 either way).
    # Every segment is BT.709 / limited range and says so explicitly, whatever its source was:
    # the final file takes its tags from the first segment, so they must all agree.
    return ["-c:v", "libx264", "-preset", enc.preset, "-crf", str(enc.crf), "-pix_fmt", "yuv420p",
            "-bf", "0", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-color_range", "tv"]


def segment_command(tools: Tools, clip: ClipSpec, plan: Plan, enc: EncodeSettings,
                    dest: Path, threads: int = 0) -> list[str]:
    """One ffmpeg process: decode, filter and encode a clip that has no frame-backend effect."""
    chain = build_chain(clip, plan)
    assert not chain.uses_frame_backend
    cmd = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           *input_args(clip, chain), "-an", "-sn", "-dn",
           "-vf", ",".join([*chain.filters, TAG_BT709]), "-frames:v", str(clip.frames), *encoder_args(enc)]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    return [*cmd, str(dest)]


def decode_command(tools: Tools, clip: ClipSpec, chain: ClipChain) -> list[str]:
    """Decode + filter a clip to raw RGB frames on stdout (for the frame backend)."""
    return [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            *input_args(clip, chain), "-an", "-sn", "-dn",
            "-vf", ",".join(chain.filters), "-frames:v", str(clip.frames),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def encode_command(tools: Tools, plan: Plan, enc: EncodeSettings, dest: Path, threads: int = 0) -> list[str]:
    """Encode raw RGB frames from stdin; identical encoder settings to the ffmpeg-only path."""
    out = plan.output
    cmd = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{out.width}x{out.height}",
           "-framerate", _fps_arg(out.fps), "-i", "pipe:0",
           "-vf", f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,{TAG_BT709}", *encoder_args(enc)]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    return [*cmd, str(dest)]


def concat_command(tools: Tools, list_file: Path, dest: Path, plan: Plan | None = None) -> list[str]:
    """Stream-copy concat of the segments, plus the music if the plan has some.

    `dest` is written as MP4 regardless of its file name. Video is copied; audio is AAC.
    Output length is fixed by `-t` (the plan's exact length): longer music is cut, shorter
    music is padded with silence (`apad`)."""
    audio = plan.audio if plan is not None else None
    cmd = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file)]
    if audio is None or audio.path is None:
        return [*cmd, "-c", "copy", "-an", "-movflags", "+faststart", "-f", "mp4", str(dest)]
    return [*cmd, "-ss", f"{audio.start:.3f}", "-i", str(audio.path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-af", "apad",
            "-t", f"{plan.output.duration:.6f}", "-movflags", "+faststart", "-f", "mp4", str(dest)]
