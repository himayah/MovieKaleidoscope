from __future__ import annotations

import threading
import time
from fractions import Fraction
from pathlib import Path

import pytest

from moviekaleidoscope.config import config_from_dict
from moviekaleidoscope.errors import Cancelled, ConfigError
from moviekaleidoscope.media.output_format import OutputFormat
from moviekaleidoscope.media.probe import VideoInfo
from moviekaleidoscope.planner.plan import Plan, load_plan, save_plan
from moviekaleidoscope.planner import planner as _planner
from moviekaleidoscope.planner.planner import (
    MIN_TAIL_FRAMES, SOURCE_MARGIN, frames_at, reselect_source, resolve_cut_mode,
)
from moviekaleidoscope.planner.rng import make_rng
from moviekaleidoscope.util.process import run_cancellable

import sys

FMT = OutputFormat(1280, 720, Fraction(30))

def build_plan(cfg, infos, fmt, seed):
    """Test shorthand: plan for cfg.duration with the cut mode the config asks for."""
    return _planner.build_plan(cfg, infos, fmt, seed, duration=cfg.duration, mode=_planner.resolve_cut_mode(cfg))


def infos(*durations: float) -> list[VideoInfo]:
    return [VideoInfo(Path(f"in{i}.mp4"), 1280, 720, Fraction(30), d) for i, d in enumerate(durations)]


def cfg(duration=10, **cut):
    return config_from_dict({"inputs": ["x"], "output": "o.mp4", "duration": duration,
                             "effects": {"enabled": ["hue"]},
                             "cut": {"mode": "fixed", "fixed": 2, **cut}})


# ---- rng ----------------------------------------------------------------------
def test_rng_deterministic_and_purpose_separated():
    assert make_rng(1, "cut").random() == make_rng(1, "cut").random()
    assert make_rng(1, "cut").random() != make_rng(1, "source").random()
    assert make_rng(1, "cut").random() != make_rng(2, "cut").random()


# ---- cut lengths ------------------------------------------------------------------
def test_fixed_cuts_tile_the_timeline():
    plan = build_plan(cfg(10), infos(30, 30), FMT, seed=1)
    assert [c.frames for c in plan.clips] == [60] * 5
    assert plan.output.frames == 300 and plan.output.duration == 10.0
    assert [c.start_frame for c in plan.clips] == [0, 60, 120, 180, 240]


def test_short_tail_is_merged_into_previous_clip():
    # 10.1 s = 303 frames: a 3-frame tail (< MIN_TAIL_FRAMES) is merged
    plan = build_plan(cfg(10.1), infos(30), FMT, seed=1)
    assert [c.frames for c in plan.clips] == [60, 60, 60, 60, 63]
    # 10.2 s = 306 frames: a 6-frame tail is kept
    assert MIN_TAIL_FRAMES <= 6
    assert [c.frames for c in build_plan(cfg(10.2), infos(30), FMT, seed=1).clips][-1] == 6


def test_random_cut_lengths_within_range():
    plan = build_plan(cfg(120, mode="random", random={"min": 1, "max": 4}), infos(30), FMT, seed=3)
    body = [c.frames for c in plan.clips[:-1]]
    assert len(set(body)) > 5
    assert all(30 - 1 <= f <= 120 + 1 for f in body)   # +-1 frame of rounding
    assert sum(c.frames for c in plan.clips) == 3600


def test_ntsc_boundaries_are_derived_from_cumulative_time():
    fps = Fraction(30000, 1001)
    plan = build_plan(cfg(100, fixed=2), infos(30), OutputFormat(640, 360, fps), seed=1)
    assert plan.output.frames == frames_at(Fraction(100), fps) == 2997
    for k, c in enumerate(plan.clips[:-1]):
        assert c.start_frame == frames_at(Fraction(2 * k), fps)   # no drift after 50 clips


def test_too_short_duration_is_an_error():
    with pytest.raises(ConfigError):
        build_plan(cfg(0.01), infos(30), FMT, seed=1)


# ---- determinism and stream independence -----------------------------------------------
def test_same_seed_same_plan_different_seed_different_plan():
    a = build_plan(cfg(30, mode="random"), infos(30, 20, 40), FMT, seed=5)
    b = build_plan(cfg(30, mode="random"), infos(30, 20, 40), FMT, seed=5)
    c = build_plan(cfg(30, mode="random"), infos(30, 20, 40), FMT, seed=6)
    assert a.to_dict() == b.to_dict()
    assert a.to_dict() != c.to_dict()


def test_cut_lengths_do_not_depend_on_the_inputs():
    one = build_plan(cfg(60, mode="random"), infos(30), FMT, seed=9)
    three = build_plan(cfg(60, mode="random"), infos(30, 30, 30), FMT, seed=9)
    assert [c.frames for c in one.clips] == [c.frames for c in three.clips]


# ---- source selection ---------------------------------------------------------------------
def test_inputs_are_used_equally():
    plan = build_plan(cfg(30, fixed=1), infos(60, 60, 60), FMT, seed=2)
    counts = {}
    for c in plan.clips:
        counts[c.source.path] = counts.get(c.source.path, 0) + 1
    assert sorted(counts.values()) == [10, 10, 10]


def test_in_points_stay_inside_the_file_and_short_inputs_loop():
    plan = build_plan(cfg(40, fixed=2), infos(10, 1.0), FMT, seed=4)
    for c in plan.clips:
        s = c.source
        if s.path.name == "in1.mp4":          # 1 s file, 2 s needed
            assert s.loop and s.in_point == 0.0
        else:
            assert not s.loop and 0 <= s.in_point <= 10 - s.span - SOURCE_MARGIN + 1e-9


def test_reselect_picks_another_input_deterministically():
    plan = build_plan(cfg(10), infos(30, 30, 30), FMT, seed=1)
    clip = plan.clips[0]
    ins = infos(30, 30, 30)
    a = reselect_source(plan, clip, ins)
    assert a is not None and a.path != clip.source.path and a.span == clip.source.span
    assert a == reselect_source(plan, clip, ins)
    assert reselect_source(plan, clip, [i for i in ins if i.path == clip.source.path]) is None


# ---- cut mode resolution -----------------------------------------------------------------------------------
def test_cut_mode_resolution():
    assert resolve_cut_mode(cfg()) == "fixed"
    auto = config_from_dict({"cut": {"mode": "auto"}})
    assert resolve_cut_mode(auto) == "fixed"                                      # no music, no BPM
    assert resolve_cut_mode(config_from_dict({"cut": {"mode": "auto"}, "audio": {"bpm": 120}})) == "beat"
    assert resolve_cut_mode(config_from_dict({"cut": {"mode": "auto"}, "audio": {"file": "a.wav"}})) == "beat"
    # an explicit mode is never changed: with music, `fixed` / `random` ignore the BPM for cutting
    assert resolve_cut_mode(config_from_dict({"cut": {"mode": "fixed"}, "audio": {"file": "a.wav"}})) == "fixed"
    assert resolve_cut_mode(config_from_dict({"cut": {"mode": "random"}, "audio": {"bpm": 90}})) == "random"
    assert resolve_cut_mode(config_from_dict({"cut": {"mode": "beat"}})) == "beat"


# ---- plan files --------------------------------------------------------------------------------------
def test_plan_roundtrip_with_unicode_paths(tmp_path):
    plan = build_plan(cfg(10), [VideoInfo(Path("/素材/日本語 動画.mp4"), 1280, 720, Fraction(30), 30)], FMT, 1)
    f = tmp_path / "sub" / "p.plan.json"
    save_plan(plan, f)
    assert load_plan(f).to_dict() == plan.to_dict()
    assert "日本語 動画.mp4" in f.read_text(encoding="utf-8")   # not \u-escaped


def _plan_dict():
    return build_plan(cfg(10), infos(30), FMT, seed=1).to_dict()


def test_plan_rejects_bad_data(tmp_path):
    d = _plan_dict()
    d["version"] = 99
    with pytest.raises(ConfigError, match="バージョン"):
        Plan.from_dict(d)
    d = _plan_dict()
    d["clips"][1]["start_frame"] += 1
    with pytest.raises(ConfigError, match="クリップ 1"):
        Plan.from_dict(d)
    d = _plan_dict()
    d["output"]["frames"] += 1
    with pytest.raises(ConfigError, match="合計"):
        Plan.from_dict(d)
    d = _plan_dict()
    del d["clips"][0]["source"]
    with pytest.raises(ConfigError, match="形式"):
        Plan.from_dict(d)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON"):
        load_plan(bad)
    with pytest.raises(ConfigError):
        load_plan(tmp_path / "missing.json")


# ---- process helper ---------------------------------------------------------------------------------------
def test_run_cancellable_returns_code_and_stderr():
    rc, err = run_cancellable([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert rc == 3 and err == "boom"


def test_run_cancellable_terminates_child_on_cancel():
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        run_cancellable([sys.executable, "-c", "import time; time.sleep(30)"], ev)
    assert time.monotonic() - t0 < 5


@pytest.mark.skipif(sys.platform != "win32", reason="launchers (Chocolatey's ffmpeg shim) are a Windows thing")
def test_cancel_stops_the_real_program_behind_a_launcher(tmp_path):
    """The ffmpeg on PATH can be a shim that runs the real ffmpeg.exe as its child. Cancelling must
    stop that child too: it kept running, and kept its output file open, after the shim was killed."""
    out = tmp_path / "seg.mp4"
    worker = f"import time; f = open({str(out)!r}, 'wb')\nfor _ in range(200): f.write(b'x'); f.flush(); time.sleep(0.05)"
    launcher = f"import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-c', {worker!r}]))"
    ev = threading.Event()

    def cancel_once_writing():
        t0 = time.monotonic()
        while not out.exists() and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        ev.set()

    threading.Thread(target=cancel_once_writing, daemon=True).start()
    with pytest.raises(Cancelled):
        run_cancellable([sys.executable, "-c", launcher], ev)
    time.sleep(0.2)
    out.unlink()        # PermissionError while the worker is still alive and holding the file
