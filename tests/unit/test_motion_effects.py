from __future__ import annotations

import math
import random
from fractions import Fraction
from pathlib import Path

import pytest

from moviekaleidoscope.config import config_from_dict
from moviekaleidoscope.effects import registry
from moviekaleidoscope.effects.base import ParamSpec, RenderContext, SampleContext, sample_motion
from moviekaleidoscope.errors import ConfigError
from moviekaleidoscope.media.output_format import OutputFormat
from moviekaleidoscope.media.probe import VideoInfo
from moviekaleidoscope.planner.motion import Motion
from moviekaleidoscope.planner.plan import EffectSpec, Plan
from moviekaleidoscope.planner import planner as _planner

CTX = RenderContext(duration=2.0, frames=60, fps=Fraction(30), width=640, height=360, bpm=None)
CTX_BPM = RenderContext(duration=2.0, frames=60, fps=Fraction(30), width=640, height=360, bpm=120.0)


def fx_cfg(**effects):
    return config_from_dict({"effects": effects}).effects


def ffmpeg_eval(expr: str, t: float) -> float:
    """Evaluate the subset of ffmpeg expression syntax our motions use."""
    env = {"t": t, "PI": math.pi, "sin": math.sin, "exp": math.exp, "min": min, "max": max,
           "sqrt": math.sqrt, "mod": math.fmod}
    return eval(expr, {"__builtins__": {}}, env)  # noqa: S307 - test-only, trusted strings


# ---- Motion ----------------------------------------------------------------------------
MOTIONS = [
    Motion.const(3.5),
    Motion("ramp", 1.0, end=-2.0),
    Motion("sine", 5.0, amp=2.0, period=1.5, phase=0.7),
    Motion("sine", 5.0, amp=2.0, period=2, unit="beat", phase=0.0),
    Motion("pulse", 1.0, amp=4.0, period=1, unit="beat", decay=3.0),
    Motion("pulse", 0.0, amp=1.0, period=0.8, decay=2.5),
]


@pytest.mark.parametrize("m", MOTIONS, ids=lambda m: f"{m.kind}-{m.unit}")
def test_expression_matches_numeric_value(m):
    for t in (0.0, 0.1, 0.37, 0.5, 1.0, 1.73, 2.0, 5.0):
        assert ffmpeg_eval(m.expr(2.0, bpm=120.0), t) == pytest.approx(m.value(t, 2.0, 120.0), abs=1e-4)


@pytest.mark.parametrize("m", MOTIONS, ids=lambda m: f"{m.kind}-{m.unit}")
def test_motion_dict_roundtrip(m):
    assert Motion.from_dict(m.to_dict()) == m


def test_ramp_is_clamped_to_the_clip():
    m = Motion("ramp", 0.0, end=10.0)
    assert m.value(-1, 2.0) == 0.0 and m.value(1.0, 2.0) == 5.0 and m.value(9, 2.0) == 10.0


def test_pulse_peaks_on_the_beat_and_decays():
    m = Motion("pulse", 1.0, amp=4.0, period=1, unit="beat", decay=3.0)    # 120 bpm: beat = 0.5 s
    assert m.value(0.0, 9, 120) == pytest.approx(5.0) and m.value(0.5, 9, 120) == pytest.approx(5.0)
    assert 1.0 < m.value(0.25, 9, 120) < 5.0


def test_beat_unit_needs_bpm():
    with pytest.raises(ValueError, match="BPM"):
        Motion("sine", 0, amp=1, period=1, unit="beat").value(0.1, 1.0, None)


@pytest.mark.parametrize("bad", [
    {"motion": "wobble", "base": 1}, {"base": 1}, {"motion": "ramp", "base": 1},
    {"motion": "sine", "base": 1, "amp": 1}, {"motion": "sine", "base": 1, "period": 0},
    {"motion": "const", "base": float("nan")}, {"motion": "const", "base": 1, "extra": 2}, "x", None,
    {"motion": "sine", "base": 1, "period": 1, "unit": "bar"},
])
def test_motion_rejects_bad_dicts(bad):
    with pytest.raises(ValueError):
        Motion.from_dict(bad)


def test_mean_is_defined_only_for_const_and_ramp():
    assert Motion.const(2).mean() == 2 and Motion("ramp", 1, end=3).mean() == 2
    with pytest.raises(ValueError):
        Motion("sine", 1, amp=1, period=1).mean()


# ---- sampling ----------------------------------------------------------------------------
@pytest.mark.parametrize("bpm", [None, 120.0])
def test_sampled_motions_stay_in_range(bpm):
    spec = ParamSpec("p", 2.0, 10.0)
    rng = random.Random(1)
    kinds = set()
    for _ in range(600):
        d = sample_motion(rng, spec, 2.0, 10.0, SampleContext(2.0, bpm))
        m = Motion.from_dict(d)
        kinds.add(m.kind)
        assert ("beat" == m.unit) == (bpm is not None and m.kind in ("sine", "pulse"))
        for t in [i * 0.05 for i in range(41)]:
            assert 2.0 - 1e-3 <= m.value(t, 2.0, bpm) <= 10.0 + 1e-3, d
    assert kinds == ({"const", "ramp", "sine"} if bpm is None else {"const", "ramp", "sine", "pulse"})


def test_log_parameters_are_sampled_log_uniformly():
    spec = ParamSpec("rate", 0.25, 4.0, motions=("const",), log=True)
    rng = random.Random(2)
    vals = [Motion.from_dict(sample_motion(rng, spec, 0.25, 4.0, SampleContext(1.0))).base for _ in range(2000)]
    assert min(vals) >= 0.25 and max(vals) <= 4.0
    assert 0.4 < sum(v < 1.0 for v in vals) / len(vals) < 0.6     # slow and fast equally likely


# ---- individual effects ----------------------------------------------------------------------
def test_speed_const_and_ramp():
    e = registry.get("speed")
    const = {"rate": Motion.const(2.0).to_dict()}
    ramp = {"rate": Motion("ramp", 1.0, end=3.0).to_dict()}
    assert e.source_span(const, 2.0) == 4.0 and e.source_span(ramp, 2.0) == 4.0     # duration * mean rate
    assert e.pre_filters(const, CTX) == ["setpts=PTS/2"]
    (f,) = e.pre_filters(ramp, CTX)
    assert f.startswith("setpts='") and "sqrt" in f
    # the setpts expression is the inverse of u(T) = a*T + (b-a)*T^2/(2D): T(u(T)) == T
    expr = f[len("setpts='"):-1].replace("PTS*TB", "u").replace("/TB", "")
    for T in (0.0, 0.5, 1.0, 2.0):
        u = 1.0 * T + (3.0 - 1.0) * T * T / (2 * 2.0)
        got = eval(expr, {"__builtins__": {}}, {"u": u, "sqrt": math.sqrt, "max": max})
        assert got == pytest.approx(T, abs=1e-6)


def test_speed_ramp_with_equal_ends_degrades_to_const():
    e = registry.get("speed")
    assert e.pre_filters({"rate": Motion("ramp", 1.5, end=1.5).to_dict()}, CTX) == ["setpts=PTS/1.5"]


def test_flip_filters():
    e = registry.get("flip")
    assert e.post_filters({"axis": "h", "alternate": None}, CTX) == ["hflip"]
    assert e.post_filters({"axis": "hv", "alternate": None}, CTX) == ["hflip", "vflip"]
    f = e.post_filters({"axis": "v", "alternate": {"period": 0.5, "unit": "sec"}}, CTX)
    assert f == ["vflip=enable='lt(mod(t,2*0.5),0.5)'"]
    f = e.post_filters({"axis": "v", "alternate": {"period": 2, "unit": "beat"}}, CTX_BPM)   # 2 beats @120 = 1 s
    assert f == ["vflip=enable='lt(mod(t,2*1),1)'"]
    with pytest.raises(ValueError, match="BPM"):
        e.post_filters({"axis": "v", "alternate": {"period": 2, "unit": "beat"}}, CTX)


def test_hue_and_saturate_use_expressions():
    h = registry.get("hue").post_filters({"angle": Motion("ramp", 0, end=360).to_dict()}, CTX)
    assert h == ["hue=h='0+360*min(1,t/2)'"]
    s = registry.get("saturate").post_filters({"gain": Motion.const(2.5).to_dict()}, CTX)
    assert s == ["hue=s='2.5'"]


def test_rgbshift_backend_depends_on_the_motion():
    e = registry.get("rgbshift")
    const = {"amount": Motion.const(10).to_dict(), "angle": 0.0}
    moving = {"amount": Motion("ramp", 1, end=5).to_dict(), "angle": 0.0}
    assert e.backend_for(const) == "ffmpeg" and e.backend_for(moving) == "frame"
    assert e.post_filters(const, CTX) == ["scale=in_color_matrix=bt709:in_range=tv", "format=gbrp",
                                          "rgbashift=rh=10:rv=0:bh=-10:bv=0", "scale=out_color_matrix=bt709:out_range=tv"]
    assert e.post_filters(moving, CTX) == []           # nothing for ffmpeg to do; Python handles it


def test_reverse_is_limited_by_length():
    e = registry.get("reverse")
    assert e.available(SampleContext(8.0), {"max_sec": 10.0})
    assert not e.available(SampleContext(12.0), {"max_sec": 10.0})
    assert e.needs_input_bound
    assert e.post_filters({}, CTX)[0] == "trim=end_frame=60" and "reverse" in e.post_filters({}, CTX)


@pytest.mark.parametrize("name", registry.names())
def test_every_effect_samples_valid_params_that_render_filters(name):
    e = registry.get(name)
    ranges = {p.name: (p.lo, p.hi) for p in e.params}
    for bpm, ctx in ((None, CTX), (120.0, CTX_BPM)):
        rng = random.Random(7)
        for _ in range(200):
            params = e.sample(rng, SampleContext(2.0, bpm), ranges, dict(e.options))
            assert isinstance(e.pre_filters(params, ctx), list) and isinstance(e.post_filters(params, ctx), list)
            assert e.source_span(params, 2.0) > 0


# ---- registry & pool ------------------------------------------------------------------------------
def test_unknown_effects_are_errors():
    with pytest.raises(ConfigError, match="未知"):
        registry.get("nope")
    with pytest.raises(ConfigError, match="未知"):
        registry.validate_effects_config(fx_cfg(enabled=["nope"]))
    assert len(registry.names()) == 15


def test_all_means_every_implemented_effect_and_exclude_removes():
    assert registry.active_names(fx_cfg()) == registry.names()
    assert "reverse" not in registry.active_names(fx_cfg(exclude=["reverse"]))
    with pytest.raises(ConfigError, match="1 つもありません"):
        registry.active_names(fx_cfg(enabled=["hue"], exclude=["hue"]))
    with pytest.raises(ConfigError, match="未知"):
        registry.active_names(fx_cfg(exclude=["typo"]))


def test_weights_select_effects():
    pool = registry.EffectPool(fx_cfg(weights={**{n: 0.0 for n in registry.names()}, "hue": 1.0}))
    rng = random.Random(1)
    assert {pool.pick(rng, SampleContext(2.0)).type for _ in range(50)} == {"hue"}
    heavy = registry.EffectPool(fx_cfg(enabled=["hue", "flip"], weights={"hue": 9.0, "flip": 1.0}))
    picks = [heavy.pick(rng, SampleContext(2.0)).type for _ in range(2000)]
    assert 0.85 < picks.count("hue") / len(picks) < 0.95
    with pytest.raises(ConfigError, match="すべて 0"):
        registry.EffectPool(fx_cfg(enabled=["hue"], weights={"hue": 0}))
    with pytest.raises(ConfigError, match="未知"):
        registry.EffectPool(fx_cfg(weights={"typo": 1}))


def test_param_range_overrides():
    pool = registry.EffectPool(fx_cfg(enabled=["rgbshift"], params={"rgbshift": {"amount": {"min": 20, "max": 20.5}}}))
    rng = random.Random(3)
    for _ in range(20):
        spec = pool.pick(rng, SampleContext(2.0))
        assert 20 <= Motion.from_dict(spec.params["amount"]).base <= 20.5


@pytest.mark.parametrize("params", [
    {"rgbshift": {"amount": {"min": 9, "max": 1}}},       # min > max
    {"rgbshift": {"amount": {"mim": 1}}},                  # typo in key
    {"rgbshift": {"amount": 5}},                           # not a range
    {"rgbshift": {"nope": {"min": 1}}},                    # unknown parameter
    {"speed": {"rate": {"min": 0}}},                       # log parameter needs min > 0
    {"reverse": {"max_sec": 0}},
    {"typo": {}},
])
def test_bad_override_is_an_error(params):
    with pytest.raises(ConfigError):
        registry.EffectPool(fx_cfg(params=params))


def test_reverse_max_sec_option():
    short_only = registry.EffectPool(fx_cfg(enabled=["reverse"]))
    with pytest.raises(ConfigError, match="使えるエフェクトがありません"):
        short_only.pick(random.Random(1), SampleContext(20.0))
    longer = registry.EffectPool(fx_cfg(enabled=["reverse"], params={"reverse": {"max_sec": 30}}))
    assert longer.pick(random.Random(1), SampleContext(20.0)).type == "reverse"


# ---- planner integration ----------------------------------------------------------------------------
FMT = OutputFormat(1280, 720, Fraction(30))


def build_plan(cfg, infos, fmt, seed):
    return _planner.build_plan(cfg, infos, fmt, seed, duration=cfg.duration, mode=_planner.resolve_cut_mode(cfg))


def infos(*durations):
    return [VideoInfo(Path(f"in{i}.mp4"), 1280, 720, Fraction(30), d) for i, d in enumerate(durations)]


def plan_cfg(duration=60, **effects):
    return config_from_dict({"inputs": ["x"], "output": "o.mp4", "duration": duration,
                             "cut": {"mode": "random"}, "effects": effects})


def test_every_clip_gets_exactly_one_effect_and_all_are_used():
    plan = build_plan(plan_cfg(200), infos(60, 60), FMT, seed=3)
    assert all(len(c.effects) == 1 for c in plan.clips)
    assert {c.effects[0].type for c in plan.clips} == set(registry.names())


def test_source_span_follows_the_effect():
    plan = build_plan(plan_cfg(200, enabled=["speed"]), infos(400), FMT, seed=3)
    for c in plan.clips:
        dur = c.frames / 30
        mean = Motion.from_dict(c.effects[0].params["rate"]).mean()
        assert c.source.span == pytest.approx(dur * mean, abs=1e-5)
    assert any(c.source.span < c.frames / 30 for c in plan.clips) and any(c.source.span > c.frames / 30 for c in plan.clips)


def test_effect_settings_do_not_change_cuts_or_input_choice():
    """SPEC §5.3: purpose-separated streams. Only the effects (and what they need) may differ."""
    a = build_plan(plan_cfg(120, enabled=["hue"]), infos(300, 300, 300), FMT, seed=8)
    b = build_plan(plan_cfg(120, enabled=["speed", "flip", "saturate"]), infos(300, 300, 300), FMT, seed=8)
    assert [c.frames for c in a.clips] == [c.frames for c in b.clips]
    assert [c.source.path for c in a.clips] == [c.source.path for c in b.clips]


def test_looping_clips_do_not_shift_other_clips_positions():
    # in1 is 1.5 s long: with some effects a clip loops (draws nothing), with others it does not
    ins = infos(300, 1.5)
    a = build_plan(plan_cfg(60, enabled=["hue"]), ins, FMT, seed=4)
    b = build_plan(plan_cfg(60, enabled=["hue", "speed"], weights={"hue": 1, "speed": 3}), ins, FMT, seed=4)
    for ca, cb in zip(a.clips, b.clips):
        if ca.source.path == cb.source.path == Path("in0.mp4") and ca.effects[0].type == cb.effects[0].type == "hue":
            assert ca.source.in_point == cb.source.in_point


def test_effects_survive_a_plan_roundtrip():
    plan = build_plan(plan_cfg(60), infos(60), FMT, seed=5)
    assert Plan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()


def test_plan_rejects_bad_effects():
    d = build_plan(plan_cfg(10, enabled=["hue"]), infos(60), FMT, seed=5).to_dict()
    d["clips"][1]["effects"] = [{"type": "nope", "params": {}}]
    with pytest.raises(ConfigError, match="未知"):
        Plan.from_dict(d)
    d["clips"][1]["effects"] = [{"type": "hue", "params": {}}]
    with pytest.raises(ConfigError, match="クリップ 1.*hue"):
        Plan.from_dict(d)
    d["clips"][1]["effects"] = [{"type": "hue", "params": {"angle": {"motion": "sine", "base": 1}}}]
    with pytest.raises(ConfigError, match="クリップ 1"):
        Plan.from_dict(d)
    d["clips"][1]["effects"] = [{"type": "hue", "params": {"angle": {"motion": "const", "base": 90}}}]
    Plan.from_dict(d)    # valid again
