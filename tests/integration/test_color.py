"""Colours must survive both render paths and every source type (SPEC §8.1: segments are BT.709).

The expected picture is the source decoded with ITS OWN matrix (explicitly, via ffmpeg);
the actual picture is the output decoded as BT.709. Both render paths must agree with it.
"""

from __future__ import annotations

import copy
import subprocess

import numpy as np
import pytest
from tools.make_dummy import make_video

from moviekaleidoscope.api import make_plan
from moviekaleidoscope.planner.plan import EffectSpec
from moviekaleidoscope.render import pipeline
from moviekaleidoscope.render.ffmpeg import EncodeSettings

pytestmark = pytest.mark.integration
W, H = 640, 360
IN_POINT = 0.5

TAG_709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
TAG_601 = ["-colorspace", "smpte170m", "-color_primaries", "smpte170m", "-color_trc", "smpte170m"]


@pytest.fixture(scope="module")
def sources(tools, tmp_path_factory):
    d = tmp_path_factory.mktemp("colorsrc")
    # (path, matrix the file really is in: tag, else HD=709 / SD=601)
    return {
        "tagged_709": (make_video(tools, d / "t709.mp4", size=f"{W}x{H}", rate="30", duration=2, extra=TAG_709), "bt709"),
        "tagged_601": (make_video(tools, d / "t601.mp4", size=f"{W}x{H}", rate="30", duration=2, extra=TAG_601), "bt601"),
        "untagged_sd": (make_video(tools, d / "usd.mp4", size=f"{W}x{H}", rate="30", duration=2), "bt601"),
        "untagged_hd": (make_video(tools, d / "uhd.mp4", size="1280x720", rate="30", duration=2), "bt709"),
    }


def decode_rgb(tools, path, matrix: str, size=(W, H), seek=0.0, nth=0) -> np.ndarray:
    """One RGB frame, converting from YCbCr with an explicit matrix (limited range)."""
    cmd = [tools.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{seek}", "-i", str(path),
           "-vf", f"scale={size[0]}:{size[1]}:in_color_matrix={matrix}:in_range=tv,format=rgb24",
           "-frames:v", str(nth + 1), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = size[0] * size[1] * 3
    return np.frombuffer(raw[nth * n:(nth + 1) * n], np.uint8).reshape(size[1], size[0], 3).astype(np.float32)


@pytest.mark.parametrize("kind", ["tagged_709", "tagged_601", "untagged_sd", "untagged_hd"])
@pytest.mark.parametrize("path_kind", ["ffmpeg", "frame"])
def test_colours_are_preserved(kind, path_kind, sources, tools, make_cfg, tmp_path):
    src, true_matrix = sources[kind]
    cfg = make_cfg([src], duration=1, cut={"mode": "fixed", "fixed": 1},
                   video={"width": W, "height": H, "crf": 8, "preset": "ultrafast"})
    plan, report = make_plan(cfg, tools)
    assert plan.clips[0].source.matrix == true_matrix          # probe picked the right matrix
    plan = copy.deepcopy(plan)
    clip = plan.clips[0]
    clip.source.in_point = IN_POINT
    # "ffmpeg" path: no effect. "frame" path: an identity effect that forces the Python route.
    clip.effects = [] if path_kind == "ffmpeg" else [EffectSpec("negate", {"mix": {"motion": "const", "base": 0.0}})]
    out = tmp_path / f"{kind}_{path_kind}.mp4"
    pipeline.render_plan(plan, tools=tools, enc=EncodeSettings(8, "ultrafast"), runtime=cfg.runtime, output=out)

    expected = decode_rgb(tools, src, true_matrix, size=(W, H), seek=IN_POINT)
    actual = decode_rgb(tools, out, "bt709")
    wrong = decode_rgb(tools, src, "bt709" if true_matrix != "bt709" else "bt601", size=(W, H), seek=IN_POINT)

    err = float(np.abs(actual - expected).mean())
    assert err < 3.0, f"colours drifted: mean error {err:.2f}"
    # the check has teeth: reading the source with the wrong matrix would be clearly different
    assert float(np.abs(wrong - expected).mean()) > 3 * err


def test_every_segment_carries_the_same_colour_tags(tools, make_cfg, sources, ffprobe_json):
    """The output's tags come from the first segment, so frame-path and ffmpeg-path segments
    must declare the same thing -- whatever their sources were tagged as."""
    from moviekaleidoscope.api import generate

    cfg = make_cfg([sources["tagged_601"][0], sources["tagged_709"][0], sources["untagged_sd"][0]], duration=8,
                   cut={"mode": "fixed", "fixed": 0.5}, seed=2, effects={"enabled": ["negate", "hue", "kaleidoscope", "flip"]},
                   video={"width": W, "height": H})
    result = generate(cfg)
    assert {c.effects[0].type for c in result.plan.clips} >= {"negate", "hue"}      # both paths used
    v = next(s for s in ffprobe_json(result.output)["streams"] if s["codec_type"] == "video")
    assert (v["color_space"], v["color_primaries"], v["color_transfer"], v["color_range"]) == \
        ("bt709", "bt709", "bt709", "tv")
