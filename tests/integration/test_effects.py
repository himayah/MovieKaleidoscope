"""Do the ffmpeg-backed effects really do what they claim? Compare decoded pixels with an
effect-free render of the same source region."""

from __future__ import annotations

import copy
import math
import subprocess

import numpy as np
import pytest

from moviekaleidoscope.api import generate, make_plan
from moviekaleidoscope.effects import registry
from moviekaleidoscope.planner.motion import Motion
from moviekaleidoscope.planner.plan import EffectSpec
from moviekaleidoscope.render import pipeline
from moviekaleidoscope.render.ffmpeg import EncodeSettings

cv2 = pytest.importorskip("cv2")
pytestmark = pytest.mark.integration

FPS = 30
N = 60                    # frames per test clip (2 s)
IN_POINT = 2.0


def read_frames(path, step: int = 1) -> list[np.ndarray]:
    """Decoded BGR frames as uint8 (every `step`-th only, to keep memory small).

    Decoded by ffmpeg as BT.709 / limited range, which is what every output is (SPEC §3.3).
    Not cv2.VideoCapture: its YCbCr matrix depends on the build (the Linux wheel follows the
    tags, the Windows one always uses BT.601), which made the same file decode differently."""
    from moviekaleidoscope.media.tools import locate_tools

    probe = subprocess.run([locate_tools().ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    w, h = map(int, probe.stdout.strip().split(","))
    raw = subprocess.run([locate_tools().ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
                          "-vf", "scale=in_color_matrix=bt709:in_range=tv,format=bgr24",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
    return [f.copy() for f in frames[::step]]


def mae(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


@pytest.fixture
def render(dummy, make_cfg, tools, tmp_path):
    """render(effects, name=..., frames=N, span=None) -> decoded BGR frames of a single clip."""
    cfg = make_cfg([dummy.base], duration=2, cut={"mode": "fixed", "fixed": 2},
                   video={"width": 320, "height": 180, "crf": 8, "preset": "ultrafast"}, runtime={"jobs": 1})
    base_plan, _ = make_plan(cfg, tools)
    assert len(base_plan.clips) == 1

    def run(effects, name, frames=N, span=None, source=None):
        plan = copy.deepcopy(base_plan)
        clip = plan.clips[0]
        if source is not None:
            clip.source.path = source
        clip.frames, plan.output.frames, plan.output.duration = frames, frames, frames / FPS
        clip.source.in_point, clip.source.loop, clip.source.span = IN_POINT, False, span or frames / FPS
        clip.effects = [EffectSpec(t, p) for t, p in effects]
        out = tmp_path / f"{name}.mp4"
        pipeline.render_plan(plan, tools=tools, enc=EncodeSettings(8, "ultrafast"), runtime=cfg.runtime, output=out)
        frames_out = read_frames(out)
        assert len(frames_out) == frames
        return frames_out

    return run


@pytest.fixture
def ref(render):
    return render([], "ref")


def best_match(frame, candidates) -> int:
    return int(np.argmin([mae(frame, c) for c in candidates]))


@pytest.fixture(scope="session")
def pastel(tools, tmp_path_factory):
    """A washed-out version of the test pattern, so that a saturation boost is measurable."""
    from tools.make_dummy import make_video

    return make_video(tools, tmp_path_factory.mktemp("pastel") / "pastel.mp4", size="640x360", rate="30",
                      duration=6, extra=["-vf", "eq=saturation=0.35"])


def mean_motion(v):
    return Motion.const(v).to_dict()


# ---- flip ------------------------------------------------------------------------------------
@pytest.mark.parametrize("axis,op", [
    ("h", lambda f: f[:, ::-1]), ("v", lambda f: f[::-1]), ("hv", lambda f: f[::-1, ::-1]),
])
def test_flip(render, ref, axis, op):
    out = render([("flip", {"axis": axis, "alternate": None})], f"flip_{axis}")
    for k in (0, 20, 59):
        assert mae(out[k], op(ref[k])) < 6
        assert mae(out[k], ref[k]) > 3 * mae(out[k], op(ref[k]))


def test_flip_alternates_in_time(render, ref):
    out = render([("flip", {"axis": "h", "alternate": {"period": 0.5, "unit": "sec"}})], "flip_alt")
    flipped = lambda k: mae(out[k], ref[k][:, ::-1]) < 6   # noqa: E731
    normal = lambda k: mae(out[k], ref[k]) < 6             # noqa: E731
    assert all(flipped(k) for k in (1, 7, 13))             # 0.0-0.5 s
    assert all(normal(k) for k in (16, 22, 28))            # 0.5-1.0 s
    assert all(flipped(k) for k in (31, 37, 43))           # 1.0-1.5 s
    assert all(normal(k) for k in (46, 52, 58))            # 1.5-2.0 s


# ---- reverse ---------------------------------------------------------------------------------
def test_reverse_plays_the_same_frames_backwards(render, ref):
    out = render([("reverse", {})], "reverse")
    for k in (0, 10, 30, 59):
        assert best_match(out[k], ref) == N - 1 - k


# ---- speed -------------------------------------------------------------------------------------
@pytest.mark.parametrize("rate", [2.0, 0.5])
def test_constant_speed(render, rate):
    span = N / FPS * rate
    long_ref = render([], f"ref_{rate}", frames=round(span * FPS))
    out = render([("speed", {"rate": mean_motion(rate)})], f"speed_{rate}", span=span)
    for k in (6, 18, 40):
        assert abs(best_match(out[k], long_ref) - k * rate) <= 1


def test_speed_ramp_follows_the_acceleration_curve(render):
    a, b, d = 1.0, 3.0, N / FPS
    span = d * (a + b) / 2                                    # 4 s of source for 2 s of output
    long_ref = render([], "ref_ramp", frames=round(span * FPS))
    out = render([("speed", {"rate": Motion("ramp", a, end=b).to_dict()})], "speed_ramp", span=span)
    for k in (6, 18, 30, 45, 58):
        t = k / FPS
        expected = (a * t + (b - a) * t * t / (2 * d)) * FPS     # source frame index
        assert abs(best_match(out[k], long_ref) - expected) <= 2, (k, expected)


# ---- colour effects -------------------------------------------------------------------------------
def hue_shift_units(a: np.ndarray, b: np.ndarray) -> float:
    """Median circular hue difference (OpenCV units: 0-179) over clearly coloured pixels."""
    ha, hb = (cv2.cvtColor(x.astype(np.uint8), cv2.COLOR_BGR2HSV) for x in (a, b))
    mask = (ha[..., 1] > 120) & (ha[..., 2] > 80) & (hb[..., 1] > 120) & (hb[..., 2] > 80)
    d = np.abs(ha[..., 0].astype(int) - hb[..., 0].astype(int))[mask]
    return float(np.median(np.minimum(d, 180 - d)))


@pytest.mark.parametrize("deg", [60, 120, 180])
def test_constant_hue_rotation(render, ref, deg):
    out = render([("hue", {"angle": mean_motion(deg)})], f"hue_{deg}")
    assert hue_shift_units(ref[30], out[30]) == pytest.approx(deg / 2, abs=4)


def test_hue_ramp_rotates_over_time(render, ref):
    """Frame k of a 0 -> 180 degree ramp over 2 s must look exactly like a constant rotation by the
    angle at that moment (180 * k/60). (HSV hue is not linear in YUV chroma rotation, so comparing
    against a measured 'expected hue' would only test the measuring.)"""
    out = render([("hue", {"angle": Motion("ramp", 0, end=180).to_dict()})], "hue_ramp")
    assert hue_shift_units(ref[0], out[0]) < 4
    assert hue_shift_units(ref[59], out[59]) == pytest.approx(88.5, abs=6)      # 177 degrees = 88.5 units
    for k in (10, 30, 45):
        const = render([("hue", {"angle": mean_motion(180 * k / 60)})], f"hue_const_{k}")
        assert mae(out[k], const[k]) < 3.0, k


def mean_chroma(f: np.ndarray) -> float:
    """Mean colour-difference magnitude (YCrCb). `hue=s=` scales exactly this, linearly;
    HSV saturation would saturate at 255 and hide the gain."""
    ycc = cv2.cvtColor(f.astype(np.uint8), cv2.COLOR_BGR2YCrCb).astype(float)
    return float(np.hypot(ycc[..., 1] - 128, ycc[..., 2] - 128).mean())


def test_saturation_boost(render, pastel):
    base = render([], "pastel_ref", source=pastel)
    out = render([("saturate", {"gain": mean_motion(2.0)})], "sat", source=pastel)
    assert mean_chroma(base[30]) < 60                                        # the source really is washed out
    assert mean_chroma(out[30]) == pytest.approx(2.0 * mean_chroma(base[30]), rel=0.1)


def test_saturation_ramp_increases_over_time(render, pastel):
    base = render([], "pastel_ref2", source=pastel)
    out = render([("saturate", {"gain": Motion("ramp", 1.0, end=3.0).to_dict()})], "sat_ramp", source=pastel)
    gain = lambda k: mean_chroma(out[k]) / mean_chroma(base[k])              # noqa: E731
    assert gain(1) == pytest.approx(1.0, abs=0.15)
    assert gain(30) == pytest.approx(2.0, rel=0.2)                           # mid-clip: 1 + 2*0.5
    assert gain(58) == pytest.approx(1 + 2 * 58 / 60, rel=0.1)


# ---- rgbshift -----------------------------------------------------------------------------------------
def best_shift(channel_out: np.ndarray, channel_ref: np.ndarray, axis: int) -> int:
    best = min(range(-14, 15), key=lambda s: mae(channel_out[20:-20, 20:-20],
                                                 np.roll(channel_ref, s, axis=axis)[20:-20, 20:-20]))
    return best


def test_rgbshift_moves_red_and_blue_in_opposite_directions(render, ref):
    out = render([("rgbshift", {"amount": mean_motion(8), "angle": 0.0})], "rgb")
    o, r = out[30], ref[30]
    shifts = {name: best_shift(o[..., i], r[..., i], axis=1) for i, name in ((0, "b"), (1, "g"), (2, "r"))}
    assert shifts["g"] == 0
    assert abs(shifts["r"]) == 8 and abs(shifts["b"]) == 8 and shifts["r"] == -shifts["b"]


def test_rgbshift_vertical(render, ref):
    out = render([("rgbshift", {"amount": mean_motion(6), "angle": 90.0})], "rgb_v")
    o, r = out[30], ref[30]
    assert best_shift(o[..., 1], r[..., 1], axis=0) == 0
    assert abs(best_shift(o[..., 2], r[..., 2], axis=0)) == 6
    assert best_shift(o[..., 2], r[..., 2], axis=1) == 0


# ---- every effect, end to end -----------------------------------------------------------------------------
@pytest.mark.parametrize("name", registry.names())
def test_each_effect_renders_a_whole_video_with_exact_timing(name, dummy, make_cfg, frame_times):
    cfg = make_cfg([dummy.base, dummy.small43, dummy.vertical], duration=10, seed=21,
                   cut={"mode": "random", "random": {"min": 0.3, "max": 2.5}},
                   effects={"enabled": [name]}, video={"fps": 30, "width": 640, "height": 360})
    result = generate(cfg)
    assert {c.effects[0].type for c in result.plan.clips} == {name}
    t = frame_times(result.output)
    assert len(t) == result.plan.output.frames == 300
    gaps = [(i, b - a) for i, (a, b) in enumerate(zip(t, t[1:])) if abs((b - a) - 1 / 30) > 0.002]
    assert gaps == []
    assert t[-1] == pytest.approx(299 / 30, abs=0.0005)


def test_all_effects_together_and_replay_from_plan(dummy, make_cfg, tools, tmp_path, frame_times):
    from moviekaleidoscope.api import load_plan, render_plan

    cfg = make_cfg([dummy.base, dummy.small43, dummy.short], duration=20, seed=33, video={"width": 640, "height": 360},
                   cut={"mode": "random", "random": {"min": 0.3, "max": 2.0}}, effects={"enabled": "all"})
    result = generate(cfg)
    assert len({c.effects[0].type for c in result.plan.clips}) >= 5        # a good mix
    again = tmp_path / "again.mp4"
    render_plan(load_plan(result.plan_path), cfg, again, tools)
    a, b = frame_times(result.output), frame_times(again)
    assert len(a) == len(b) == 600 and a == b
    fa, fb = read_frames(result.output, step=25), read_frames(again, step=25)
    assert len(fa) == len(fb) == 24
    assert max(mae(x, y) for x, y in zip(fa, fb)) < 1.0                      # same plan, same picture


# ---- frame backend: the whole pipe, effect by effect ---------------------------------------------------------------------
from fractions import Fraction                                  # noqa: E402

from moviekaleidoscope.effects.base import RenderContext, SampleContext   # noqa: E402
import random                                                    # noqa: E402

FRAME_BACKEND_EFFECTS = ["negate", "mirror", "solarize", "posterize", "zoom", "kaleidoscope", "strobe",
                         "feedback", "tile", "rgbshift"]
OUT_W, OUT_H = 320, 180


def sample_frame_params(name: str, seed: int):
    """Sampled parameters that use the frame backend (rgbshift: skip the constant draws)."""
    effect = registry.get(name)
    ranges = {p.name: (p.lo, p.hi) for p in effect.params}
    rng = random.Random(seed)
    while True:
        params = effect.sample(rng, SampleContext(2.0), ranges, dict(effect.options))
        if effect.backend_for(params) == "frame":
            return params


def codec_floor(tools, frames_bgr, tmp_path) -> float:
    """Mean error that encoding these frames (same encoder settings, BT.709 4:2:0) costs by itself."""
    from types import SimpleNamespace

    from moviekaleidoscope.render.ffmpeg import encode_command

    plan = SimpleNamespace(output=SimpleNamespace(width=OUT_W, height=OUT_H, fps=Fraction(FPS)))
    dest = tmp_path / "floor.mp4"
    cmd = encode_command(tools, plan, EncodeSettings(8, "ultrafast"), dest)
    raw = b"".join(cv2.cvtColor(f, cv2.COLOR_BGR2RGB).tobytes() for f in frames_bgr)
    subprocess.run(cmd, input=raw, check=True, capture_output=True)
    decoded = read_frames(dest)
    assert len(decoded) == len(frames_bgr)
    return float(np.mean([mae(a, b) for a, b in zip(frames_bgr, decoded)]))


@pytest.mark.parametrize("name", FRAME_BACKEND_EFFECTS)
@pytest.mark.parametrize("seed", [1, 2])
def test_pipe_equals_processor_applied_to_the_reference(name, seed, render, ref, tools, tmp_path):
    """The video written through the Python pipe must match what the effect's processor does to
    the same decoded frames: this pins channel order, per-frame time, frame alignment and state."""
    params = sample_frame_params(name, seed)
    out = render([(name, params)], f"pipe_{name}_{seed}")
    ctx = RenderContext(N / FPS, N, Fraction(FPS), OUT_W, OUT_H, None)
    process = registry.get(name).frame_processor(params, ctx)
    small = lambda f: cv2.resize(f, (OUT_W // 4, OUT_H // 4), interpolation=cv2.INTER_AREA)   # noqa: E731
    errors, coarse, process_all = [], [], []
    for n in range(N):
        rgb = process(cv2.cvtColor(ref[n], cv2.COLOR_BGR2RGB), n / FPS, n)
        process_all.append(rgb)
        expected = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        errors.append(mae(out[n], expected))
        coarse.append(mae(small(out[n]), small(expected)))
    # Coarse structure (4x downscaled) must match tightly: any misplaced or mistimed content shows here.
    assert float(np.mean(coarse)) < 3.0, (params, np.round(coarse[::10], 1))
    # Full resolution: some effects (tile, kaleidoscope) create fine colour detail that 4:2:0 chroma
    # subsampling cannot keep (encoding the *expected* frames alone costs ~9 for tile, ~0.1 for plain
    # frames). So measure that floor with the same encoder and require the pipe to stay close to it.
    floor = codec_floor(tools, [cv2.cvtColor(process_all[n], cv2.COLOR_RGB2BGR) for n in range(N)], tmp_path)
    assert float(np.mean(errors)) <= 1.3 * floor + 1.5, (params, round(float(np.mean(errors)), 2), round(floor, 2))


def test_tile_scroll_follows_the_clock(render, ref):
    """Position and timing, without magnification: one tile per second to the right means
    frame k shows the reference shifted by k/30 of its width."""
    out = render([("tile", {"transform": "scroll", "wrap": "repeat", "tile_n": 1, "scroll_angle": 0.0,
                            "speed": mean_motion(1.0)})], "tile_scroll")
    for k in (0, 6, 15, 30, 45, 59):
        shift = round(k / FPS * OUT_W)
        assert mae(out[k], np.roll(ref[k], -shift, axis=1)) < 6.0, k
    assert mae(out[15], np.roll(ref[15], -round(14 / FPS * OUT_W), axis=1)) > 3 * 6.0 / 3   # a one-frame error would show


def test_rgbshift_frame_and_ffmpeg_backends_agree_on_direction(render, ref):
    for angle in (0.0, 90.0, 45.0):
        a = math.radians(angle)
        const = render([("rgbshift", {"amount": mean_motion(8), "angle": angle})], f"rgb_ff_{angle}")
        moving = render([("rgbshift", {"amount": Motion("ramp", 8, end=8).to_dict(), "angle": angle})], f"rgb_fr_{angle}")
        # same picture (integer vs sub-pixel shifts differ only a little, and by rounding at 45 degrees)
        assert np.mean([mae(const[k], moving[k]) for k in (5, 30, 55)]) < 5.0, angle
        if angle in (0.0, 90.0):
            axis = 1 if angle == 0.0 else 0
            for c in (0, 2):
                assert best_shift(const[30][..., c], ref[30][..., c], axis) == best_shift(moving[30][..., c], ref[30][..., c], axis)


def test_negate_strobe_and_mirror_look_right(render, ref):
    neg = render([("negate", {"mix": mean_motion(1.0)})], "neg1")
    assert mae(neg[30], 255 - ref[30]) < 4
    half = render([("mirror", {"mode": "lr", "pos": mean_motion(0.5)})], "mir")
    assert mae(half[30][:, OUT_W // 2:], half[30][:, :OUT_W // 2][:, ::-1]) < 6
    flash = render([("strobe", {"rate": mean_motion(5.0), "mode": "black", "duty": 0.5})], "strobe")
    # 5 Hz at 30 fps = 6-frame cycles; "on" while phase < 0.5: frames 0-2 dark, 3-5 normal, 6-8 dark ...
    assert all(flash[k].mean() < 10 for k in (0, 1, 2, 6, 7, 8)) and all(mae(flash[k], ref[k]) < 4 for k in (3, 4, 5, 9))


# ---- frame backend: robustness --------------------------------------------------------------------------------------------------------
def test_frame_effect_on_a_looped_short_input(dummy, make_cfg, tools):
    cfg = make_cfg([dummy.short], duration=4, cut={"mode": "fixed", "fixed": 2}, effects={"enabled": ["kaleidoscope", "tile"]})
    result = generate(cfg)
    assert all(c.source.loop for c in result.plan.clips)
    assert len(read_frames(result.output, step=1)) == 120


def test_failing_effect_is_a_clear_render_error_and_cleans_up(dummy, make_cfg, tools, tmp_path, monkeypatch):
    from moviekaleidoscope.errors import RenderError

    def boom(self, params, ctx):
        def process(frame, t, n):
            if n == 3:
                raise ZeroDivisionError("simulated bug")
            return frame
        return process

    monkeypatch.setattr(registry.get("negate").__class__, "frame_processor", boom)
    cfg = make_cfg([dummy.base], duration=2, effects={"enabled": ["negate"]})
    with pytest.raises(RenderError, match="フレーム 3.*ZeroDivisionError"):
        generate(cfg)
    assert not (tmp_path / "out" / "result.mp4").exists()
    work = tmp_path / "work"
    assert not work.exists() or list(work.iterdir()) == []


def test_source_that_ends_early_is_an_error_not_a_truncated_video(dummy, make_cfg, tools):
    from moviekaleidoscope.errors import RenderError

    cfg = make_cfg([dummy.base], duration=2, cut={"mode": "fixed", "fixed": 2}, effects={"enabled": ["negate"]})
    plan, _ = make_plan(cfg, tools)
    plan.clips[0].source.in_point, plan.clips[0].source.loop = 9.5, False      # only 0.5 s left in the file
    with pytest.raises(RenderError, match="フレーム"):
        pipeline.render_plan(plan, tools=tools, enc=EncodeSettings(30, "ultrafast"), runtime=cfg.runtime, output=cfg.output)


def test_cancel_during_a_frame_render_stops_the_ffmpeg_processes(dummy, make_cfg, tools, tmp_path, monkeypatch):
    import shutil
    import subprocess
    import sys
    import threading
    import time

    from moviekaleidoscope.errors import Cancelled

    started: list[subprocess.Popen] = []

    class RecordingPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)   # works on every OS (no pgrep)
    # Not "ultrafast": a slower preset (the default is medium) keeps frames in flight, so an encoder
    # that survives the cancel (e.g. the real ffmpeg behind a Chocolatey shim) still holds its file.
    cfg = make_cfg([dummy.base], duration=40, cut={"mode": "fixed", "fixed": 20},
                   effects={"enabled": ["kaleidoscope"]}, video={"width": 1280, "height": 720, "preset": "veryfast"})
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        generate(cfg, cancel=cancel)
    assert time.monotonic() - t0 < 15
    assert started                                              # the frame pipes really ran
    assert [p.args for p in started if p.poll() is None] == []  # no orphaned decoder / encoder
    if sys.platform != "win32" and shutil.which("pgrep"):       # also from the OS's point of view
        time.sleep(0.3)
        leftover = subprocess.run(["pgrep", "-f", str(tmp_path / "work")], capture_output=True).stdout
        assert leftover == b""
    assert not (tmp_path / "out" / "result.mp4").exists()
    work = tmp_path / "work"                                    # nothing still writing into the work folder
    assert not work.exists() or list(work.rglob("*")) == []
