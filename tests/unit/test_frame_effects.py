"""Frame-backend effects on small synthetic frames: exact geometric / value properties."""

from __future__ import annotations

import random
from fractions import Fraction

import numpy as np
import pytest

from moviekaleidoscope.effects import registry
from moviekaleidoscope.effects.base import RenderContext, SampleContext, phase_fn
from moviekaleidoscope.planner.motion import Motion

W, H = 64, 48
CTX = RenderContext(duration=2.0, frames=60, fps=Fraction(30), width=W, height=H, bpm=None)
RNG = np.random.RandomState(0)
FRAME = RNG.randint(0, 256, (H, W, 3)).astype(np.uint8)


def const(v):
    return Motion.const(v).to_dict()


def proc(name, params, ctx=CTX):
    return registry.get(name).frame_processor(params, ctx)


FRAME_EFFECTS = [n for n in registry.names() if registry.get(n).backend == "frame"]


# ---- generic contract -----------------------------------------------------------------------------
def test_there_are_nine_frame_effects_plus_animated_rgbshift():
    assert sorted(FRAME_EFFECTS) == sorted(["negate", "mirror", "solarize", "posterize", "zoom", "kaleidoscope",
                                            "strobe", "feedback", "tile"])
    assert registry.get("rgbshift").backend_for({"amount": Motion("ramp", 1, end=3).to_dict(), "angle": 0}) == "frame"


@pytest.mark.parametrize("name", [*FRAME_EFFECTS, "rgbshift"])
@pytest.mark.parametrize("bpm", [None, 120.0])
def test_sampled_parameters_always_work(name, bpm):
    effect = registry.get(name)
    ranges = {p.name: (p.lo, p.hi) for p in effect.params}
    ctx = RenderContext(2.0, 60, Fraction(30), W, H, bpm)
    rng = random.Random(11)
    tried = 0
    for _ in range(80):
        params = effect.sample(rng, SampleContext(2.0, bpm), ranges, dict(effect.options))
        if effect.backend_for(params) != "frame":
            continue
        tried += 1
        a, b = effect.frame_processor(params, ctx), effect.frame_processor(params, ctx)   # twice: determinism
        snapshot = FRAME.copy()
        for n in range(0, 60, 7):
            ya, yb = a(FRAME, n / 30, n), b(FRAME, n / 30, n)
            assert ya.shape == (H, W, 3) and ya.dtype == np.uint8
            assert np.array_equal(ya, yb)
        assert np.array_equal(FRAME, snapshot), "an effect modified its input frame"
    assert tried > 20


# ---- helper functions -----------------------------------------------------------------------------------
def test_phase_integrates_const_and_ramp_rates():
    e = registry.get("strobe")
    c = phase_fn(e, {"rate": const(5.0)}, "rate", CTX)
    assert c(0.4) == pytest.approx(2.0)
    r = phase_fn(e, {"rate": Motion("ramp", 2.0, end=6.0).to_dict()}, "rate", CTX)     # 2 -> 6 Hz over 2 s
    assert r(0) == 0 and r(1.0) == pytest.approx(2 * 1 + 4 * 1 / 4) and r(2.0) == pytest.approx(8.0)
    assert r(3.0) == pytest.approx(8.0 + 6.0)                                           # holds 6 Hz afterwards
    with pytest.raises(ValueError, match="const か ramp"):
        phase_fn(e, {"rate": Motion("sine", 4, amp=1, period=1).to_dict()}, "rate", CTX)


# ---- negate ---------------------------------------------------------------------------------------------------
def test_negate():
    f = lambda mix: proc("negate", {"mix": const(mix)})(FRAME, 0.3, 9)       # noqa: E731
    assert np.array_equal(f(0.0), FRAME)
    assert np.array_equal(f(1.0), 255 - FRAME)
    half = f(0.5).astype(int)
    assert np.abs(half - (FRAME.astype(int) + (255 - FRAME.astype(int))) / 2).max() <= 1
    ramp = proc("negate", {"mix": Motion("ramp", 0.0, end=1.0).to_dict()})
    assert np.array_equal(ramp(FRAME, 0.0, 0), FRAME) and np.array_equal(ramp(FRAME, 2.0, 59), 255 - FRAME)


# ---- mirror ----------------------------------------------------------------------------------------------------
def mirror(mode, pos=0.5):
    return proc("mirror", {"mode": mode, "pos": const(pos)})(FRAME, 0.0, 0)


def test_mirror_halves():
    left, right, top, bottom = FRAME[:, :32], FRAME[:, 32:], FRAME[:24], FRAME[24:]
    o = mirror("lr");   assert np.array_equal(o[:, :32], left) and np.array_equal(o[:, 32:], left[:, ::-1])    # noqa: E702
    o = mirror("rl");   assert np.array_equal(o[:, 32:], right) and np.array_equal(o[:, :32], right[:, ::-1])  # noqa: E702
    o = mirror("tb");   assert np.array_equal(o[:24], top) and np.array_equal(o[24:], top[::-1])               # noqa: E702
    o = mirror("bt");   assert np.array_equal(o[24:], bottom) and np.array_equal(o[:24], bottom[::-1])         # noqa: E702
    q = mirror("quad")
    assert np.array_equal(q[:24, :32], FRAME[:24, :32])
    assert np.array_equal(q[:24, 32:], FRAME[:24, :32][:, ::-1]) and np.array_equal(q[24:], q[:24][::-1])


def test_mirror_off_centre_reflects_about_the_axis_and_survives_extremes():
    o = mirror("lr", 0.3)                                  # axis at x = 19
    assert np.array_equal(o[:, :19], FRAME[:, :19])
    assert np.array_equal(o[:, 19], FRAME[:, 18]) and np.array_equal(o[:, 20], FRAME[:, 17])
    assert o.shape == FRAME.shape
    for pos in (-1.0, 0.0, 1.0, 3.0):                      # clamped, never an index error
        for mode in ("lr", "rl", "tb", "bt", "quad"):
            assert mirror(mode, pos).shape == FRAME.shape
    o = mirror("rl", 0.3)                                  # keep [19, 64): left side shows the right, reflected
    assert np.array_equal(o[:, 19:], FRAME[:, 19:]) and np.array_equal(o[:, 18], FRAME[:, 19])


# ---- solarize / posterize -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("channels", ["rgb", "r", "g", "b", "rg", "rb", "gb"])
def test_solarize(channels):
    o = proc("solarize", {"threshold": const(100), "channels": channels})(FRAME, 0, 0)
    expected = np.where(FRAME >= 100, 255 - FRAME, FRAME).astype(np.uint8)
    for i, ch in enumerate("rgb"):
        assert np.array_equal(o[..., i], expected[..., i] if ch in channels else FRAME[..., i]), ch


def test_solarize_threshold_moves():
    p = proc("solarize", {"threshold": Motion("ramp", 0, end=255).to_dict(), "channels": "rgb"})
    assert np.array_equal(p(FRAME, 0.0, 0), 255 - FRAME)                       # threshold 0: everything inverted
    assert np.array_equal(p(FRAME, 2.0, 59), FRAME | 0) or (p(FRAME, 2.0, 59) != FRAME).sum() < FRAME.size * 0.01


@pytest.mark.parametrize("levels", [2, 3, 4, 8])
def test_posterize(levels):
    o = proc("posterize", {"levels": const(levels)})(FRAME, 0, 0)
    for c in range(3):
        assert len(np.unique(o[..., c])) <= levels
    assert o.min() == 0 and o.max() == 255
    assert np.abs(o.astype(int) - FRAME.astype(int)).max() <= 256 // levels + 1     # moves a value to its band's level


# ---- zoom ------------------------------------------------------------------------------------------------------------------------
def test_zoom():
    ident = proc("zoom", {"scale": const(1.0), "cx": const(0.5), "cy": const(0.5)})(FRAME, 0, 0)
    assert np.array_equal(ident, FRAME)
    z2 = proc("zoom", {"scale": const(2.0), "cx": const(0.5), "cy": const(0.5)})(FRAME, 0, 0)
    assert np.array_equal(z2[24, 32], FRAME[24, 32])                           # the centre does not move
    assert np.array_equal(z2[24, 32 + 2], FRAME[24, 32 + 1])                   # twice as big: 1 source px = 2 out px
    for s in (0.3, 0.5, 5.0):                                                  # zoom out / in far: reflected borders
        assert proc("zoom", {"scale": const(s), "cx": const(0.3), "cy": const(0.7)})(FRAME, 0, 0).shape == FRAME.shape


# ---- kaleidoscope -----------------------------------------------------------------------------------------------------------------------------
def test_kaleidoscope_is_mirror_symmetric_about_its_axis():
    frame = RNG.randint(0, 256, (49, 65, 3)).astype(np.uint8)
    ctx = RenderContext(2.0, 60, Fraction(30), 65, 49, None)
    for segs in (3, 6, 8):
        o = proc("kaleidoscope", {"segments": segs, "angle": const(0), "cx": const(0.5), "cy": const(0.5)}, ctx)(frame, 0, 0)
        # centre (32.5, 24.5): rows y and 49-y are mirror images, so they must show the same thing
        assert np.abs(o[1:].astype(int) - o[1:][::-1].astype(int)).mean() < 1.0, segs
        assert np.abs(o.astype(int) - frame.astype(int)).mean() > 20           # and it really changed the picture


def test_kaleidoscope_rotates_and_moves():
    base = {"segments": 6, "cx": const(0.5), "cy": const(0.5)}
    a = proc("kaleidoscope", {**base, "angle": const(0)})(FRAME, 0, 0)
    b = proc("kaleidoscope", {**base, "angle": const(30)})(FRAME, 0, 0)
    assert not np.array_equal(a, b)
    r = proc("kaleidoscope", {**base, "angle": Motion("ramp", 0, end=360).to_dict()})
    assert not np.array_equal(r(FRAME, 0.0, 0), r(FRAME, 1.0, 30)) and np.array_equal(r(FRAME, 0.0, 0), a)
    mv = proc("kaleidoscope", {"segments": 5, "angle": const(0), "cx": Motion("ramp", 0.4, end=0.6).to_dict(),
                               "cy": const(0.5)})
    assert not np.array_equal(mv(FRAME, 0.0, 0), mv(FRAME, 2.0, 59))


@pytest.mark.parametrize("segments", [1, 0, "x", 100, None])
def test_kaleidoscope_rejects_bad_segments(segments):
    with pytest.raises(ValueError):
        proc("kaleidoscope", {"segments": segments, "angle": const(0), "cx": const(.5), "cy": const(.5)})


# ---- strobe ---------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode,expected", [("black", lambda f: np.zeros_like(f)), ("white", lambda f: np.full_like(f, 255)),
                                           ("invert", lambda f: 255 - f)])
def test_strobe(mode, expected):
    p = proc("strobe", {"rate": const(5.0), "mode": mode, "duty": 0.5})       # 5 Hz: on for the first 0.1 s of each 0.2 s
    assert np.array_equal(p(FRAME, 0.05, 1), expected(FRAME))
    assert np.array_equal(p(FRAME, 0.15, 4), FRAME)
    assert np.array_equal(p(FRAME, 0.25, 7), expected(FRAME))


def test_strobe_validates_parameters():
    for bad in ({"mode": "red", "duty": 0.3}, {"mode": "black", "duty": 0}, {"mode": "black", "duty": 1}):
        with pytest.raises(ValueError):
            proc("strobe", {"rate": const(5.0), **bad})


def test_strobe_accelerates():
    p = proc("strobe", {"rate": Motion("ramp", 2.0, end=10.0).to_dict(), "mode": "black", "duty": 0.5})
    dark = [not p(FRAME, n / 30, n).any() for n in range(60)]

    def flashes(lo, hi):          # number of flashes starting in frames [lo, hi)
        return sum(1 for n in range(lo, hi) if dark[n] and (n == 0 or not dark[n - 1]))

    # 2 -> 10 Hz over 2 s: ~4 flashes in the first second, ~8 in the second
    assert flashes(0, 30) in (3, 4, 5) and flashes(30, 60) in (7, 8, 9)


# ---- feedback --------------------------------------------------------------------------------------------------------------------------------------------
def test_feedback_keeps_a_fading_trail_and_resets_per_clip():
    params = {"decay": const(0.5), "zoom": const(1.0), "rot": const(0.0)}
    p = proc("feedback", params)
    bright, dark = np.full((H, W, 3), 200, np.uint8), np.zeros((H, W, 3), np.uint8)
    assert np.array_equal(p(bright, 0, 0), bright)                             # first frame: nothing to remember yet
    second = p(dark, 1 / 30, 1)
    assert np.abs(second.astype(int) - 100).max() <= 1                         # trail = 50 % of the previous output
    third = p(dark, 2 / 30, 2)
    assert np.abs(third.astype(int) - 50).max() <= 1                           # ... and it keeps fading
    assert np.array_equal(proc("feedback", params)(dark, 0, 0), dark)          # a new clip starts clean


def test_feedback_never_darkens_and_zero_decay_is_identity():
    p = proc("feedback", {"decay": const(0.9), "zoom": const(1.03), "rot": const(2.0)})
    prev = None
    for n in range(8):
        f = RNG.randint(0, 256, (H, W, 3)).astype(np.uint8)
        out = p(f, n / 30, n)
        assert np.all(out >= f)
    q = proc("feedback", {"decay": const(0.0), "zoom": const(1.05), "rot": const(3.0)})
    for n in range(4):
        f = RNG.randint(0, 256, (H, W, 3)).astype(np.uint8)
        assert np.array_equal(q(f, n / 30, n), f)


# ---- tile -----------------------------------------------------------------------------------------------------------------------------------------------------------------
def tile(ctx=CTX, **kw):
    params = {"transform": "none", "wrap": "repeat", "tile_n": 1, **kw}
    return proc("tile", params, ctx)


def test_tile_identity_and_repeat():
    assert np.array_equal(tile()(FRAME, 0, 0), FRAME)                          # one tile = the picture itself
    o = tile(tile_n=2)(FRAME, 0, 0)                                            # 2x2 tiles: every other pixel, repeated
    ys, xs = np.mgrid[0:H, 0:W]
    assert np.array_equal(o, FRAME[(2 * ys - H // 2) % H, (2 * xs - W // 2) % W])


def test_tile_mirror_wrap_is_continuous_across_seams():
    o = tile(tile_n=3, wrap="mirror")(FRAME, 0, 0)
    assert o.shape == FRAME.shape
    r = tile(tile_n=3, wrap="repeat")(FRAME, 0, 0)
    assert not np.array_equal(o, r)
    # mirrored tiling has no hard edges: neighbouring pixels differ about as much as inside the source
    jump = np.abs(np.diff(o.astype(int), axis=1)).mean()
    assert jump < np.abs(np.diff(FRAME.astype(int), axis=1)).mean() * 3


def test_tile_scrolls_by_tiles_per_second():
    p = tile(transform="scroll", scroll_angle=0.0, speed=const(1.0))           # 1 tile/s to the right of the texture
    o = p(FRAME, 0.25, 7)                                                      # a quarter tile = 16 px
    assert np.array_equal(o, np.roll(FRAME, -16, axis=1))
    assert np.array_equal(p(FRAME, 1.0, 30), FRAME)                            # a whole tile: back to the start
    down = tile(transform="scroll", scroll_angle=90.0, speed=const(1.0))(FRAME, 0.5, 15)
    assert np.abs(down.astype(int) - np.roll(FRAME, -24, axis=0).astype(int)).max() <= 1


def test_tile_rotates():
    ctx = RenderContext(2.0, 60, Fraction(30), 48, 48, None)
    sq = RNG.randint(0, 256, (48, 48, 3)).astype(np.uint8)
    p = tile(ctx, transform="rotate", angle=const(90))
    o = p(sq, 0, 0)
    ys, xs = np.mgrid[0:48, 0:48]
    assert np.abs(o.astype(int) - sq[xs, (48 - ys) % 48].astype(int)).max() <= 1
    spin = tile(ctx, transform="rotate", angle=Motion("ramp", 0, end=360).to_dict())
    assert np.array_equal(spin(sq, 0, 0), sq) and not np.array_equal(spin(sq, 1.0, 30), sq)


def test_tile_sampling_only_asks_for_what_it_uses():
    effect = registry.get("tile")
    ranges = {p.name: (p.lo, p.hi) for p in effect.params}
    seen = set()
    rng = random.Random(4)
    for _ in range(200):
        p = effect.sample(rng, SampleContext(2.0), ranges, {})
        seen.add(p["transform"])
        assert ("speed" in p) == ("scroll" in p["transform"]) and ("angle" in p) == ("rotate" in p["transform"])
    assert seen == {"none", "scroll", "rotate", "scroll+rotate"}


@pytest.mark.parametrize("bad", [{"transform": "spin"}, {"wrap": "clamp"}, {"tile_n": 0}, {"tile_n": 99}])
def test_tile_rejects_bad_parameters(bad):
    with pytest.raises(ValueError):
        tile(**bad)


# ---- rgbshift (moving amount) -----------------------------------------------------------------------------------------------------------------------------------
def test_rgbshift_moving_amount_shifts_red_and_blue_apart():
    p = proc("rgbshift", {"amount": Motion("ramp", 6.0, end=6.0).to_dict(), "angle": 0.0})   # a "moving" 6 px
    o = p(FRAME, 0.5, 15)
    assert np.array_equal(o[..., 1], FRAME[..., 1])                                          # green stays
    assert np.array_equal(o[:, 6:, 0], FRAME[:, :-6, 0]) and np.array_equal(o[:, :-6, 2], FRAME[:, 6:, 2])
    grow = proc("rgbshift", {"amount": Motion("ramp", 0.0, end=10.0).to_dict(), "angle": 90.0})
    assert np.array_equal(grow(FRAME, 0.0, 0), FRAME)
    o = grow(FRAME, 2.0, 59)                                                                  # 10 px, vertical
    assert np.array_equal(o[10:, :, 0], FRAME[:-10, :, 0]) and np.array_equal(o[:-10, :, 2], FRAME[10:, :, 2])
