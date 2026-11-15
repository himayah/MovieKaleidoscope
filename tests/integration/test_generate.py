from __future__ import annotations

import json
import threading
from fractions import Fraction

import pytest

from moviekaleidoscope.api import generate, load_plan, make_plan, render_plan
from moviekaleidoscope.cli import main
from moviekaleidoscope.effects import registry
from moviekaleidoscope.errors import Cancelled, RenderError
from moviekaleidoscope.render import pipeline
from moviekaleidoscope.render.ffmpeg import EncodeSettings

pytestmark = pytest.mark.integration


def video_stream(info: dict) -> dict:
    return next(s for s in info["streams"] if s["codec_type"] == "video")


def assert_clean(tmp_path):
    work = tmp_path / "work"
    assert not work.exists() or list(work.iterdir()) == []      # temp dirs removed
    assert not list((tmp_path / "out").glob("*.part"))


def test_generate_end_to_end(dummy, make_cfg, ffprobe_json, tmp_path):
    cfg = make_cfg([dummy.base, dummy.small43, dummy.vertical, dummy.short, dummy.truncated])
    result = generate(cfg)

    out = result.output
    assert out.is_file() and result.plan_path.is_file()
    assert len(result.inputs.skipped) == 1
    info = ffprobe_json(out, count_frames=True)
    v = video_stream(info)
    assert (v["width"], v["height"]) == (1280, 720)        # first input decides; others cropped to fill
    assert v["r_frame_rate"] == "30/1"
    assert int(v["nb_frames"]) == int(v["nb_read_frames"]) == 180
    assert not [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert float(info["format"]["duration"]) == pytest.approx(6.0, abs=1 / 30 + 1e-3)
    assert_clean(tmp_path)


def test_ntsc_output_is_frame_exact(dummy, make_cfg, ffprobe_json, tmp_path):
    cfg = make_cfg([dummy.ntsc], duration=7, cut={"mode": "random", "random": {"min": 0.5, "max": 2.5}})
    out = generate(cfg).output
    v = video_stream(ffprobe_json(out, count_frames=True))
    assert v["r_frame_rate"] == "30000/1001"
    assert int(v["nb_frames"]) == int(v["nb_read_frames"]) == 210     # round(7 * 30000/1001)
    assert_clean(tmp_path)


@pytest.mark.parametrize("inputs,fps", [
    (["base", "small43", "vertical"], Fraction(30)),      # mixed 30/24 fps sources, many cuts
    (["ntsc", "base"], Fraction(30000, 1001)),            # fractional output rate
])
def test_cuts_do_not_disturb_timing(dummy, make_cfg, frame_times, inputs, fps):
    """Regression: a seek that lands between frames used to leave a one-frame gap at the start
    of every segment, so the output grew by about one frame per cut."""
    cfg = make_cfg([getattr(dummy, n) for n in inputs], duration=15, video={"fps": str(fps), "width": 640, "height": 360},
                   cut={"mode": "random", "random": {"min": 0.3, "max": 1.5}}, seed=11)
    result = generate(cfg)
    assert len(result.plan.clips) >= 12
    t = frame_times(result.output)
    step = float(1 / fps)
    assert len(t) == result.plan.output.frames
    gaps = [(i + 1, round(b - a, 4)) for i, (a, b) in enumerate(zip(t, t[1:])) if abs((b - a) - step) > 0.002]
    assert gaps == []
    # No drift either: the last frame sits where frame_count * frame_duration says (<0.5 ms).
    # (The MP4 header keeps segment durations in whole milliseconds; concat must not trust them.)
    assert t[0] == 0.0 and t[-1] == pytest.approx((len(t) - 1) * step, abs=0.0005)


def test_short_input_is_looped(dummy, make_cfg, ffprobe_json):
    cfg = make_cfg([dummy.short], duration=4, cut={"mode": "fixed", "fixed": 2})
    result = generate(cfg)
    assert all(c.source.loop for c in result.plan.clips)
    v = video_stream(ffprobe_json(result.output, count_frames=True))
    assert (v["width"], v["height"]) == (320, 180) and int(v["nb_read_frames"]) == 120


def test_resolution_and_fps_overrides(dummy, make_cfg, ffprobe_json):
    cfg = make_cfg([dummy.base, dummy.vertical], duration=3, video={"width": 640, "height": 640, "fps": 24})
    v = video_stream(ffprobe_json(generate(cfg).output, count_frames=True))
    assert (v["width"], v["height"], v["r_frame_rate"]) == (640, 640, "24/1")
    assert int(v["nb_read_frames"]) == 72


def test_same_seed_gives_identical_plan_file(dummy, make_cfg, tmp_path):
    a = generate(make_cfg([dummy.base, dummy.small43], cut={"mode": "random"}), plan_only=True)
    first = a.plan_path.read_text(encoding="utf-8")
    cfg_b = make_cfg([dummy.base, dummy.small43], cut={"mode": "random"}, overwrite=True)
    b = generate(cfg_b, plan_only=True)
    assert b.plan_path.read_text(encoding="utf-8") == first
    assert not (tmp_path / "out" / "result.mp4").exists()          # plan-only renders nothing


def test_render_from_saved_plan(dummy, make_cfg, ffprobe_json, tmp_path):
    cfg = make_cfg([dummy.base, dummy.small43])
    planned = generate(cfg, plan_only=True)
    plan = load_plan(planned.plan_path)
    out = tmp_path / "again.mp4"
    assert render_plan(plan, cfg, out) == []
    assert int(video_stream(ffprobe_json(out))["nb_frames"]) == plan.output.frames == 180


# ---- failure handling -------------------------------------------------------------------------------
def _fail_for(bad_path, calls):
    def renderer(tools, clip, plan, enc, dest, threads, cancel):
        calls.append(clip.index)
        if clip.source.path == bad_path:
            raise RenderError("simulated decode failure")
        pipeline.render_segment(tools, clip, plan, enc, dest, threads, cancel)
    return renderer


def test_failed_clip_is_retried_once_with_another_input(dummy, make_cfg, tools, tmp_path):
    cfg = make_cfg([dummy.base, dummy.small43], duration=9)
    plan, report = make_plan(cfg, tools)
    originally_bad = [c.index for c in plan.clips if c.source.path == dummy.small43]
    assert originally_bad

    replaced = pipeline.render_plan(
        plan, tools=tools, enc=EncodeSettings(30, "ultrafast"), runtime=cfg.runtime,
        output=cfg.output, inputs=report.valid, segment_renderer=_fail_for(dummy.small43, []))
    assert replaced == originally_bad
    assert all(c.source.path == dummy.base for c in plan.clips)     # plan records what was really used
    assert cfg.output.is_file()
    assert_clean(tmp_path)


def test_failure_without_inputs_is_fatal_and_cleans_up(dummy, make_cfg, tools, tmp_path):
    cfg = make_cfg([dummy.base], duration=3)
    plan, _ = make_plan(cfg, tools)
    with pytest.raises(RenderError, match="simulated"):
        pipeline.render_plan(plan, tools=tools, enc=EncodeSettings(30, "ultrafast"), runtime=cfg.runtime,
                             output=cfg.output, segment_renderer=_fail_for(dummy.base, []))
    assert not cfg.output.exists()
    assert_clean(tmp_path)


def test_failure_when_no_other_input_exists(dummy, make_cfg, tools, tmp_path):
    cfg = make_cfg([dummy.base], duration=3)
    plan, report = make_plan(cfg, tools)
    with pytest.raises(RenderError):
        pipeline.render_plan(plan, tools=tools, enc=EncodeSettings(30, "ultrafast"), runtime=cfg.runtime,
                             output=cfg.output, inputs=report.valid,
                             segment_renderer=_fail_for(dummy.base, []))
    assert_clean(tmp_path)


def test_cancel_stops_and_cleans_up(dummy, make_cfg, tmp_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        generate(make_cfg([dummy.base]), cancel=ev)
    assert not (tmp_path / "out" / "result.mp4").exists()
    assert_clean(tmp_path)


def test_keep_temp_leaves_the_work_dir(dummy, make_cfg, tmp_path):
    generate(make_cfg([dummy.base], duration=2, runtime={"keep_temp": True}))
    kept = list((tmp_path / "work").iterdir())
    assert len(kept) == 1 and (kept[0] / "seg_00001.mp4").is_file() and (kept[0] / "concat.txt").is_file()


# ---- CLI -----------------------------------------------------------------------------------------------
def test_cli_generates_and_reports_progress(dummy, tmp_path, ffprobe_json):
    out = tmp_path / "cli.mp4"
    rc = main(["-i", str(dummy.base), "-i", str(dummy.small43), "-o", str(out), "-d", "3",
               "--cut-length", "1", "--seed", "1", "--jobs", "2", "-q"])
    assert rc == 0 and out.is_file()
    plan = json.loads((tmp_path / "cli.mp4.plan.json").read_text(encoding="utf-8"))
    assert plan["seed"] == 1 and len(plan["clips"]) == 3 and plan["cut_mode"] == "fixed"
    assert int(video_stream(ffprobe_json(out))["nb_frames"]) == 90


def test_cli_refuses_to_overwrite_without_y(dummy, tmp_path):
    out = tmp_path / "o.mp4"
    args = ["-i", str(dummy.base), "-o", str(out), "-d", "2", "-q"]
    assert main(args) == 0
    assert main(args) == 2
    assert main([*args, "-y"]) == 0


def test_cli_plan_only_then_from_plan(dummy, tmp_path):
    out = tmp_path / "o.mp4"
    plan_file = tmp_path / "my.plan.json"
    assert main(["-i", str(dummy.base), "--plan", str(plan_file), "-d", "2", "--plan-only", "-q"]) == 0
    assert plan_file.is_file() and not out.exists()

    assert main(["--from-plan", str(plan_file), "-o", str(out), "-q"]) == 0
    assert out.is_file()
    assert main(["--from-plan", str(plan_file), "-o", str(out), "-q"]) == 2      # exists, no -y
    assert main(["--from-plan", str(plan_file), "-o", str(out), "-y", "-q"]) == 0


@pytest.mark.parametrize("extra", [["--seed", "3"], ["-d", "5"], ["-i", "x.mp4"], ["--fps", "24"],
                                   ["--cut-length", "2"], ["--audio", "a.wav"]])
def test_cli_from_plan_rejects_content_options(dummy, tmp_path, extra):
    plan_file = tmp_path / "p.json"
    assert main(["-i", str(dummy.base), "--plan", str(plan_file), "-d", "2", "--plan-only", "-q"]) == 0
    assert main(["--from-plan", str(plan_file), "-o", str(tmp_path / "o.mp4"), "-q", *extra]) == 2


def test_cli_beat_cut_with_manual_bpm_and_no_music(dummy, tmp_path, ffprobe_json):
    out = tmp_path / "o.mp4"
    base = ["-i", str(dummy.base), "-o", str(out), "-d", "4", "-q"]
    assert main([*base, "--cut-mode", "beat", "--bpm", "120"]) == 0
    plan = json.loads((tmp_path / "o.mp4.plan.json").read_text(encoding="utf-8"))
    assert plan["cut_mode"] == "beat" and plan["audio"]["path"] is None and plan["audio"]["bpm"] == 120
    assert not [s for s in ffprobe_json(out)["streams"] if s["codec_type"] == "audio"]
    assert main([*base, "-y", "--cut-mode", "beat"]) == 2                     # beat needs music or a BPM


# ---- effect options on the CLI ---------------------------------------------------------------------------
def test_cli_list_effects(capsys):
    assert main(["--list-effects"]) == 0
    out = capsys.readouterr().out
    for name in ("flip", "hue", "saturate", "speed", "reverse", "rgbshift"):
        assert name in out
    for name in ("negate", "mirror", "solarize", "posterize", "zoom", "kaleidoscope", "strobe", "feedback", "tile"):
        assert name in out
    assert "rate: 0.25〜4" in out and "未実装" not in out


def test_cli_effect_selection_and_errors(dummy, tmp_path):
    base = ["-i", str(dummy.base), "-d", "2", "--cut-length", "1", "-q", "--plan-only"]

    def types(plan_file):
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        return {c["effects"][0]["type"] for c in plan["clips"]}

    only = tmp_path / "only.json"
    assert main([*base, "--plan", str(only), "--effects", "flip,hue"]) == 0
    assert types(only) <= {"flip", "hue"}

    excl = tmp_path / "excl.json"
    keep = {"rgbshift", "tile"}
    drop = ",".join(n for n in registry.names() if n not in keep)
    assert main([*base, "--plan", str(excl), "-d", "40", "--exclude-effects", drop]) == 0
    assert types(excl) == keep

    assert main([*base, "--plan", str(tmp_path / "a.json"), "--effects", "kaleidoscope"]) == 0
    assert main([*base, "--plan", str(tmp_path / "b.json"), "--effects", "typo"]) == 2
    assert main([*base, "--plan", str(tmp_path / "c.json"), "--effects", "hue", "--exclude-effects", "hue"]) == 2
    assert (tmp_path / "a.json").exists()
    assert not (tmp_path / "b.json").exists() and not (tmp_path / "c.json").exists()   # failed before writing anything


def test_cli_from_plan_rejects_effect_options(dummy, tmp_path):
    plan_file = tmp_path / "p.json"
    assert main(["-i", str(dummy.base), "--plan", str(plan_file), "-d", "2", "--plan-only", "-q"]) == 0
    for extra in (["--effects", "hue"], ["--exclude-effects", "hue"]):
        assert main(["--from-plan", str(plan_file), "-o", str(tmp_path / "o.mp4"), "-q", *extra]) == 2


def test_hand_edited_plan_is_rendered_as_edited(dummy, tmp_path, ffprobe_json):
    """The plan is the contract: change an effect in the JSON and the render follows."""
    plan_file = tmp_path / "p.json"
    assert main(["-i", str(dummy.base), "--plan", str(plan_file), "-d", "2", "--plan-only", "-q"]) == 0
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    for c in plan["clips"]:
        c["effects"] = [{"type": "flip", "params": {"axis": "h", "alternate": None}}]
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--from-plan", str(plan_file), "-o", str(tmp_path / "o.mp4"), "-q"]) == 0
    assert int(video_stream(ffprobe_json(tmp_path / "o.mp4"))["nb_frames"]) == plan["output"]["frames"]

    plan["clips"][0]["effects"] = [{"type": "flip", "params": {"axis": "sideways"}}]     # invalid -> clear error
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--from-plan", str(plan_file), "-o", str(tmp_path / "o2.mp4"), "-q"]) == 2


# ---- hardening: replay checks, output problems, progress -----------------------------------------------------------------
def test_from_plan_checks_that_inputs_still_exist(dummy, tmp_path):
    plan_file = tmp_path / "p.json"
    assert main(["-i", str(dummy.base), "--plan", str(plan_file), "-d", "2", "--plan-only", "-q"]) == 0
    original = plan_file.read_text(encoding="utf-8")
    out = tmp_path / "o.mp4"

    plan = json.loads(original)
    for c in plan["clips"]:
        c["source"]["path"] = str(tmp_path / "gone.mp4")
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--from-plan", str(plan_file), "-o", str(out), "-q"]) == 3      # no input: exit 3
    assert not out.exists()

    plan = json.loads(original)
    plan["audio"] = {"path": str(tmp_path / "gone.wav"), "start": 0.0, "bpm": None, "beat_offset": None}
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--from-plan", str(plan_file), "-o", str(out), "-q"]) == 2      # no music: usage error
    assert not out.exists()


def test_output_in_an_unwritable_place_is_a_clean_error(dummy, tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")                                   # a file where a folder is needed
    assert main(["-i", str(dummy.base), "-o", str(blocker / "o.mp4"), "-d", "2", "-q"]) == 1


def test_segment_progress_is_in_frames_and_never_goes_backwards(dummy, make_cfg):
    calls = []
    cfg = make_cfg([dummy.base, dummy.small43], duration=6, cut={"mode": "random", "random": {"min": 0.4, "max": 1.5}},
                   runtime={"jobs": 3})
    result = generate(cfg, progress=lambda s, c, t: calls.append((s, c, t)))
    seg = [(c, t) for s, c, t in calls if s == "セグメント生成"]
    assert seg[0] == (0, 180) and seg[-1] == (180, 180) and len(result.plan.clips) > 4
    assert [c for c, _ in seg] == sorted(c for c, _ in seg) and all(t == 180 for _, t in seg)
    stages = []
    for s, *_ in calls:
        if not stages or stages[-1] != s:
            stages.append(s)
    assert stages == ["入力検証", "計画", "セグメント生成", "連結"]               # in order, each stage contiguous
