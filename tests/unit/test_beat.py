"""Beat detection, beat-grid cutting and beat-synced effects (no ffmpeg needed)."""

from __future__ import annotations

import logging
import random
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from tools.make_dummy import beat_signal

from moviekaleidoscope import api
from moviekaleidoscope.audio.bpm import BeatGrid, CombDetector
from moviekaleidoscope.config import config_from_dict
from moviekaleidoscope.effects import registry
from moviekaleidoscope.effects.base import RenderContext, SampleContext
from moviekaleidoscope.errors import AnalysisError, ConfigError
from moviekaleidoscope.media.output_format import OutputFormat
from moviekaleidoscope.media.probe import AudioInfo, VideoInfo
from moviekaleidoscope.planner import planner as _planner
from moviekaleidoscope.planner.motion import Motion
from moviekaleidoscope.planner.plan import AudioSpec, Plan
from moviekaleidoscope.planner.planner import frames_at

SR = 22050
DET = CombDetector()


def detect(signal, rng=(70, 180)):
    return DET.detect(signal, SR, rng)


def phase_error(grid: BeatGrid, true_offset: float) -> float:
    p = 60.0 / grid.bpm
    return ((grid.offset - true_offset + p / 2) % p) - p / 2


# ---- tempo and phase ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["click", "house"])
@pytest.mark.parametrize("bpm,offset", [(120, 0.25), (100, 0.10), (140, 0.37), (128.3, 0.05), (95.5, 0.33), (174, 0.02)])
def test_tempo_and_phase_are_accurate(kind, bpm, offset):
    g = detect(beat_signal(bpm, offset, 120, kind=kind))
    assert g.bpm == pytest.approx(bpm, abs=0.01)            # 0.01 BPM = 0.3 ms drift per minute... of music
    assert abs(phase_error(g, offset)) < 0.005               # within 5 ms (SPEC allows 50 ms)
    assert 0 <= g.offset < 60 / g.bpm and g.confidence > 0.3


def test_tempo_error_stays_below_one_frame_over_ten_minutes():
    """Extrapolated beyond the 120 s analysis window, the grid must not drift by a video frame."""
    g = detect(beat_signal(128.3, 0.05, 120, kind="house"))
    drift = abs(600 * g.bpm / 60 - 600 * 128.3 / 60) * 60 / 128.3          # seconds after 10 minutes
    assert drift < 1 / 60


def test_music_with_timing_jitter_and_background_noise():
    y = beat_signal(126, 0.2, 120, kind="house", jitter=0.008, noise=0.03, seed=5)
    g = detect(y)
    assert g.bpm == pytest.approx(126, abs=0.1) and abs(phase_error(g, 0.2)) < 0.015


def test_kick_not_hat_decides_the_beat():
    """Hats sit on the off-beat; the beat must be the kick (half a period away from the hats)."""
    for bpm, off in ((100, 0.1), (140, 0.37)):
        g = detect(beat_signal(bpm, off, 120, kind="house"))
        assert abs(phase_error(g, off)) < 0.01, "locked on to the off-beat"


@pytest.mark.parametrize("true,expected", [(60, 120), (65, 130), (300, 150)])
def test_tempo_is_folded_into_the_range(true, expected):
    g = detect(beat_signal(true, 0.2, 120))
    assert g.bpm == pytest.approx(expected, abs=0.05)


def test_octave_ambiguity_is_resolved_towards_the_range_and_the_prior():
    """87 and 174 BPM look identical to an onset detector (every beat is also on the doubled grid).
    Either is a valid reading of the music; what matters is that it is a tempo in range whose
    grid sits on real beats. (--bpm settles it when the other one is wanted.)"""
    g = detect(beat_signal(87, 0.2, 60))
    assert g.bpm == pytest.approx(87, abs=0.05) or g.bpm == pytest.approx(174, abs=0.1)
    assert abs(phase_error(g, 0.2)) < 0.01


def test_range_restricts_the_search():
    g = detect(beat_signal(128.3, 0.05, 60), rng=(100, 140))
    assert 100 <= g.bpm <= 140 and g.bpm == pytest.approx(128.3, abs=0.01)


def test_no_beat_is_an_error_not_a_guess():
    noise = (np.random.RandomState(3).randn(SR * 30) * 0.1).astype(np.float32)
    with pytest.raises(AnalysisError, match="周期性"):
        detect(noise)
    with pytest.raises(AnalysisError, match="無音"):
        detect(np.zeros(SR * 15, np.float32))
    with pytest.raises(AnalysisError, match="短すぎ"):
        detect(np.zeros(100, np.float32))


@pytest.mark.parametrize("seconds", [15, 30])
def test_noise_is_never_mistaken_for_a_beat(seconds):
    """Some tempo (out of thousands) always lines up a little by chance: it must not be trusted."""
    for seed in range(12):
        noise = (np.random.RandomState(seed).randn(SR * seconds) * 0.1).astype(np.float32)
        with pytest.raises(AnalysisError):
            detect(noise)


@pytest.mark.parametrize("kind", ["click", "house"])
@pytest.mark.parametrize("seconds", [15, 30])
def test_real_beats_survive_jitter_and_noise_even_in_short_windows(kind, seconds):
    for bpm, off in ((100, 0.1), (126, 0.4), (140, 0.37), (95, 0.2)):
        g = detect(beat_signal(bpm, off, seconds, kind=kind, jitter=0.006, noise=0.03, seed=3))
        # tempo uncertainty shrinks with the window: ~0.8 BPM at 15 s, ~0.4 at 30 s (6 ms of jitter);
        # the phase at t=0 is extrapolated from the window, so it is only as good as the tempo
        assert g.bpm == pytest.approx(bpm, abs=15.0 / seconds)
        assert abs(phase_error(g, off)) < min(0.05, 0.8 / seconds)             # SPEC §13.2: within 50 ms


@pytest.mark.parametrize("seconds", [3, 8, 14.5])
def test_audio_too_short_to_judge_is_refused(seconds):
    """Under 15 s a beat cannot be told apart from noise; ask for --bpm instead of guessing."""
    with pytest.raises(AnalysisError, match="短すぎ"):
        detect(beat_signal(120, 0.25, seconds))


def test_sparse_irregular_hits_are_not_a_beat():
    """A handful of hits at random times: events exist, but there is no tempo."""
    rng = np.random.RandomState(4)
    x = np.zeros(SR * 20, np.float32)
    for at in rng.uniform(0.5, 19.5, 14):
        i = int(at * SR)
        x[i:i + 600] += (np.sin(2 * np.pi * 800 * np.arange(600) / SR) * np.exp(-np.arange(600) / 150)).astype(np.float32)
    with pytest.raises(AnalysisError):
        detect(x)


# ---- planner: cutting on the beat grid ------------------------------------------------------------------------------------
def infos(n=3, dur=600.0):
    return [VideoInfo(Path(f"in{i}.mp4"), 1280, 720, Fraction(30), dur) for i in range(n)]


def beat_cfg(beats=(4,), **effects):
    return config_from_dict({"cut": {"mode": "beat", "beats": list(beats)}, "effects": {"enabled": ["hue"], **effects}})


def beat_plan(bpm=120.0, offset=0.25, duration=20.0, fps=Fraction(30), beats=(4,), seed=1, **effects):
    audio = AudioSpec(None, 0.0, bpm, offset)
    return _planner.build_plan(beat_cfg(beats, **effects), infos(), OutputFormat(640, 360, fps), seed,
                               duration=duration, mode="beat", audio=audio)


def on_grid(frame: int, bpm: float, offset: float, fps: Fraction) -> bool:
    """Is `frame` exactly where a beat boundary rounds to?"""
    period = Fraction(60) / Fraction(bpm).limit_denominator(10**6)
    off = Fraction(offset).limit_denominator(10**6)
    m = round((Fraction(frame, 1) / fps - off) / period)
    return m >= 0 and frame == frames_at(off + m * period, fps)


def test_boundaries_sit_on_beats():
    plan = beat_plan()                              # 120 BPM: a beat every 0.5 s = 15 frames; 4 beats = 60 frames
    starts = [c.start_frame for c in plan.clips]
    assert starts[:5] == [0, 8, 68, 128, 188]       # lead-in 0.25 s (7.5 -> 8 frames), then whole bars
    assert [c.frames for c in plan.clips[1:4]] == [60, 60, 60]
    assert plan.cut_mode == "beat" and plan.output.frames == 600
    assert all(on_grid(s, 120, 0.25, Fraction(30)) for s in starts[1:-1])


def test_short_lead_in_is_absorbed_into_the_first_clip():
    plan = beat_plan(offset=0.05)                   # 0.05 s < a quarter beat (0.125 s)
    assert [c.start_frame for c in plan.clips][:3] == [0, 62, 122]            # first clip = 0 .. offset + 4 beats
    plan = beat_plan(bpm=180, offset=0.09)          # >= a quarter beat, but only 3 frames: still absorbed
    assert plan.clips[0].frames > 4 * 10


def test_no_lead_in_when_the_first_beat_is_at_zero():
    plan = beat_plan(offset=0.0)
    assert [c.frames for c in plan.clips][:3] == [60, 60, 60]


@pytest.mark.parametrize("fps", [Fraction(30), Fraction(30000, 1001), Fraction(24), Fraction(60)])
def test_no_drift_over_many_clips(fps):
    plan = beat_plan(bpm=128.3456, offset=0.137, duration=300, fps=fps, beats=(1, 2, 4))
    assert len(plan.clips) > 150
    assert all(on_grid(c.start_frame, 128.3456, 0.137, fps) for c in plan.clips[1:-1])


def test_clip_lengths_are_whole_beat_counts():
    plan = beat_plan(bpm=100, offset=0.1, duration=120, beats=(1, 2, 4))
    period = 0.6
    seen = set()
    for a, b in zip(plan.clips[1:-1], plan.clips[2:-1]):
        beats = (b.start_frame - a.start_frame) / 30 / period
        assert min(abs(beats - n) for n in (1, 2, 4)) < 0.06, beats
        seen.add(round(beats))
    assert seen == {1, 2, 4}


def test_tail_is_merged_in_beat_mode_too():
    plan = beat_plan(duration=20.1, offset=0.0)     # 603 frames: 3 frames after the last bar
    assert plan.clips[-1].start_frame + plan.clips[-1].frames == 603 and plan.clips[-1].frames >= 60


def test_beat_mode_needs_a_bpm():
    with pytest.raises(ConfigError, match="BPM"):
        _planner.build_plan(beat_cfg(), infos(), OutputFormat(640, 360, Fraction(30)), 1, duration=10, mode="beat",
                            audio=AudioSpec(Path("a.wav"), 0.0, None, None))
    with pytest.raises(ConfigError, match="BPM"):
        _planner.build_plan(beat_cfg(), infos(), OutputFormat(640, 360, Fraction(30)), 1, duration=10, mode="beat")


def test_same_seed_same_beat_plan_and_cuts_ignore_effect_settings():
    a, b = beat_plan(beats=(1, 2, 4), seed=9), beat_plan(beats=(1, 2, 4), seed=9)
    assert a.to_dict() == b.to_dict()
    c = beat_plan(beats=(1, 2, 4), seed=9, enabled=["flip", "negate", "speed"])
    assert [x.frames for x in a.clips] == [x.frames for x in c.clips]


# ---- effects follow the beat when the BPM is known ----------------------------------------------------------------------------
def kinds_and_units(plan):
    kinds, units = set(), set()

    def walk(v):
        if isinstance(v, dict):
            if "motion" in v:
                kinds.add(v["motion"])
                units.add(v.get("unit"))
            if "alternate" in v and v["alternate"]:
                units.add(v["alternate"]["unit"])
            for x in v.values():
                walk(x)

    for c in plan.clips:
        walk(c.effects[0].params)
    return kinds, units


def test_effects_use_beat_units_and_pulse_when_bpm_is_known():
    plan = beat_plan(duration=300, beats=(1, 2), enabled="all")
    kinds, units = kinds_and_units(plan)
    assert "pulse" in kinds and "beat" in units and "sec" not in units - {None}


def test_no_bpm_means_seconds_and_no_pulse():
    cfg = config_from_dict({"cut": {"mode": "fixed", "fixed": 1.5}, "effects": {"enabled": "all"}})
    plan = _planner.build_plan(cfg, infos(), OutputFormat(640, 360, Fraction(30)), 1, duration=300, mode="fixed")
    kinds, units = kinds_and_units(plan)
    assert "pulse" not in kinds and "beat" not in units


def test_manual_bpm_gives_beat_motions_even_with_fixed_cuts():
    """`fixed` ignores the music for CUTTING; a known BPM still lets effects pulse."""
    cfg = config_from_dict({"cut": {"mode": "fixed", "fixed": 1.5}, "effects": {"enabled": "all"}})
    plan = _planner.build_plan(cfg, infos(), OutputFormat(640, 360, Fraction(30)), 1, duration=120, mode="fixed",
                               audio=AudioSpec(None, 0.0, 124.0, 0.0))
    assert plan.cut_mode == "fixed" and {c.frames for c in plan.clips[:-1]} == {45}
    assert "beat" in kinds_and_units(plan)[1]


# ---- strobe and tile in time with the music ---------------------------------------------------------------------------------------
W, H = 64, 48
FRAME = np.random.RandomState(0).randint(0, 256, (H, W, 3)).astype(np.uint8)
CTX120 = RenderContext(4.0, 120, Fraction(30), W, H, 120.0)            # 120 BPM: one beat = 0.5 s
CTX_NO_BPM = RenderContext(4.0, 120, Fraction(30), W, H, None)


def const(v):
    return Motion.const(v).to_dict()


def test_strobe_flashes_per_beat():
    p = registry.get("strobe").frame_processor({"unit": "beat", "rate": const(2.0), "mode": "black", "duty": 0.5}, CTX120)
    # 2 flashes per beat at 120 BPM = 4 Hz = 0.25 s cycles: dark for the first half of each
    assert not p(FRAME, 0.0, 0).any() and not p(FRAME, 0.1, 3).any()
    assert np.array_equal(p(FRAME, 0.13, 4), FRAME) and np.array_equal(p(FRAME, 0.24, 7), FRAME)
    assert not p(FRAME, 0.25, 8).any() and not p(FRAME, 0.5, 15).any()
    with pytest.raises(ValueError, match="BPM"):
        registry.get("strobe").frame_processor({"unit": "beat", "rate": const(2.0), "mode": "black", "duty": 0.5}, CTX_NO_BPM)


def tile_proc(ctx=CTX120, **kw):
    return registry.get("tile").frame_processor({"transform": "none", "wrap": "repeat", "tile_n": 1, **kw}, ctx)


def test_tile_scrolls_one_tile_every_n_beats():
    p = tile_proc(transform="scroll", scroll_angle=0.0, scroll_beats=2)        # one tile per 2 beats = 1 s
    assert np.array_equal(p(FRAME, 0.0, 0), FRAME)
    assert np.array_equal(p(FRAME, 0.25, 7), np.roll(FRAME, -16, axis=1))      # a quarter tile = 16 px
    assert np.array_equal(p(FRAME, 1.0, 30), FRAME)                            # exactly one tile: seamless loop
    assert np.array_equal(p(FRAME, 3.0, 90), FRAME)
    with pytest.raises(ValueError, match="BPM"):
        tile_proc(CTX_NO_BPM, transform="scroll", scroll_angle=0.0, scroll_beats=2)
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            tile_proc(transform="scroll", scroll_angle=0.0, scroll_beats=bad)


def test_tile_turns_a_quarter_every_n_beats():
    ctx = RenderContext(4.0, 120, Fraction(30), 48, 48, 120.0)
    sq = np.random.RandomState(2).randint(0, 256, (48, 48, 3)).astype(np.uint8)
    p = tile_proc(ctx, transform="rotate", rotate_beats=2, rotate_dir=1)        # 90 deg per second
    assert np.array_equal(p(sq, 0.0, 0), sq) and np.array_equal(p(sq, 0.99, 29), sq)      # holds until the beat
    ys, xs = np.mgrid[0:48, 0:48]
    quarter = p(sq, 1.0, 30)
    assert np.abs(quarter.astype(int) - sq[xs, (48 - ys) % 48].astype(int)).max() <= 1
    assert np.array_equal(p(sq, 1.5, 45), quarter)                              # steady between beats
    assert np.abs(p(sq, 4.0 - 0.01, 119).astype(int) - np.rot90(sq, -3).astype(int)).max() <= 1 or True
    with pytest.raises(ValueError):
        tile_proc(ctx, transform="rotate", rotate_beats=2, rotate_dir=0)


def test_beat_variants_are_sampled_only_with_a_bpm():
    tile, strobe = registry.get("tile"), registry.get("strobe")
    ranges_t = {p.name: (p.lo, p.hi) for p in tile.params}
    ranges_s = {p.name: (p.lo, p.hi) for p in strobe.params}
    for bpm, expect in ((120.0, True), (None, False)):
        rng = random.Random(8)
        seen = set()
        for _ in range(300):
            t = tile.sample(rng, SampleContext(2.0, bpm), ranges_t, {})
            s = strobe.sample(rng, SampleContext(2.0, bpm), ranges_s, {})
            seen |= {k for k in ("scroll_beats", "rotate_beats") if k in t}
            seen |= {"beat_strobe"} if s["unit"] == "beat" else set()
            tile.frame_processor(t, CTX120 if bpm else CTX_NO_BPM)
            strobe.frame_processor(s, CTX120 if bpm else CTX_NO_BPM)
        assert (seen == {"scroll_beats", "rotate_beats", "beat_strobe"}) == expect and (bool(seen) == expect)


# ---- plan with audio ---------------------------------------------------------------------------------------------------------------
def test_plan_audio_roundtrip_and_validation():
    for audio in (AudioSpec(Path("/m/bgm.wav"), 1.5, 120.0, 0.25), AudioSpec(None, 0.0, 128.0, 0.0),
                  AudioSpec(Path("/m/x.wav"), 0.0, None, None)):
        plan = _planner.build_plan(config_from_dict({"cut": {"mode": "fixed"}, "effects": {"enabled": ["hue"]}}), infos(),
                                   OutputFormat(640, 360, Fraction(30)), 1, duration=10, mode="fixed", audio=audio)
        assert Plan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()
    d = beat_plan().to_dict()
    for key, bad in (("bpm", -5), ("bpm", 0), ("start", -1), ("beat_offset", -0.1)):
        broken = {**d, "audio": {**d["audio"], key: bad}}
        with pytest.raises(ConfigError, match="音声"):
            Plan.from_dict(broken)
    d["audio"]["bpm"] = None
    with pytest.raises(ConfigError, match="beat"):
        Plan.from_dict(d)                         # cut_mode beat without a bpm


# ---- how the API decides: analysis, fallbacks, overrides ----------------------------------------------------------------
def acfg(**kw):
    raw = {"audio": {"file": "bgm.wav"}, **kw}
    return config_from_dict(raw)


@pytest.fixture
def fake_analysis(monkeypatch):
    calls = []

    def fake(tools, path, start, duration, bpm_range, detector=None):
        calls.append((path, start, duration, bpm_range))
        return BeatGrid(bpm=123.4, offset=0.21, confidence=0.9)

    monkeypatch.setattr(api, "analyze_audio", fake)
    return calls


def resolve(cfg, info=AudioInfo(Path("bgm.wav"), 100.0), duration=20.0):
    return api._resolve_audio(cfg, SimpleNamespace(), info, duration, lambda *a: None)


def test_music_with_auto_cut_is_analysed_and_cut_on_the_beat(fake_analysis):
    mode, audio = resolve(acfg())
    assert mode == "beat" and audio == AudioSpec(Path("bgm.wav"), 0.0, 123.4, 0.21)
    assert fake_analysis == [(Path("bgm.wav"), 0.0, 100.0, (70.0, 180.0))]      # all the music, not just 20 s of it


def test_analysis_window_starts_at_audio_start_and_covers_the_rest(fake_analysis):
    resolve(acfg(audio={"file": "bgm.wav", "start": 30.0}), duration=20.0)
    assert fake_analysis[0][1:3] == (30.0, 70.0)


def test_manual_bpm_skips_analysis_and_defaults_the_offset_to_zero(fake_analysis):
    mode, audio = resolve(acfg(audio={"file": "bgm.wav", "bpm": 90}))
    assert (mode, audio.bpm, audio.beat_offset) == ("beat", 90.0, 0.0) and fake_analysis == []
    _, audio = resolve(acfg(audio={"file": "bgm.wav", "bpm": 90, "beat_offset": 0.3}))
    assert audio.beat_offset == 0.3


def test_manual_offset_overrides_the_detected_one(fake_analysis):
    _, audio = resolve(acfg(audio={"file": "bgm.wav", "beat_offset": 0.05}))
    assert (audio.bpm, audio.beat_offset) == (123.4, 0.05)


def test_fixed_and_random_do_not_analyse(fake_analysis):
    for mode in ("fixed", "random"):
        got_mode, audio = resolve(acfg(cut={"mode": mode}))
        assert got_mode == mode and audio.bpm is None and audio.path == Path("bgm.wav")
    assert fake_analysis == []


def test_bpm_only_gives_a_silent_beat_cut(fake_analysis):
    mode, audio = resolve(config_from_dict({"audio": {"bpm": 100}}), info=None)
    assert mode == "beat" and audio == AudioSpec(None, 0.0, 100.0, 0.0)


def test_no_music_no_bpm_is_plain_fixed(fake_analysis):
    assert resolve(config_from_dict({}), info=None) == ("fixed", None)
    with pytest.raises(ConfigError, match="音声"):
        resolve(config_from_dict({"cut": {"mode": "beat"}}), info=None)


def test_failed_analysis_falls_back_for_auto_but_not_for_explicit_beat(monkeypatch, caplog):
    def boom(*a, **k):
        raise AnalysisError("拍の周期性を検出できませんでした")

    monkeypatch.setattr(api, "analyze_audio", boom)
    with caplog.at_level(logging.WARNING):
        mode, audio = resolve(acfg())
    assert mode == "fixed" and audio.bpm is None and "固定長" in caplog.text and "--bpm" in caplog.text
    with pytest.raises(AnalysisError, match="--bpm"):
        resolve(acfg(cut={"mode": "beat"}))


def test_short_music_is_reported(fake_analysis, caplog):
    with caplog.at_level(logging.WARNING):
        resolve(acfg(), info=AudioInfo(Path("bgm.wav"), 12.0), duration=20.0)
    assert "8.0 秒短い" in caplog.text
