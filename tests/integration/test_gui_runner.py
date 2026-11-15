"""The GUI's background runner and settings model, driven without any window."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from moviekaleidoscope.config import config_from_dict
from moviekaleidoscope.errors import ConfigError
from moviekaleidoscope.gui import model
from moviekaleidoscope.gui.model import GuiSettings
from moviekaleidoscope.gui.runner import JobRunner, generate_job, replay_job

pytestmark = pytest.mark.integration

# Absolute on every OS ("/" or "C:\"). A bare "/clips" is drive-relative on Windows and comes back as "C:\clips".
ABS = Path(Path.cwd().anchor)


def wait_for_end(runner: JobRunner, timeout=120.0) -> list:
    """Collect events until the job's final event."""
    events, t0 = [], time.monotonic()
    while time.monotonic() - t0 < timeout:
        events += runner.poll()
        if events and events[-1][0] in ("done", "failed", "cancelled"):
            return events
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {[e[0] for e in events][-5:]}")


# ---- runner ---------------------------------------------------------------------------------------------------
def test_job_reports_progress_logs_and_result(dummy, make_cfg):
    cfg = make_cfg([dummy.base, dummy.small43], duration=3)
    runner = JobRunner()
    runner.start(generate_job(cfg))
    events = wait_for_end(runner)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and kinds.count("done") == 1
    stages = []
    for k, p in events:
        if k == "progress" and (not stages or stages[-1] != p[0]):
            stages.append(p[0])
    assert stages == ["入力検証", "計画", "セグメント生成", "連結"]
    assert any(k == "log" and "出力しました" not in p[1] and "seed=" in p[1] for k, p in events)   # the plan line
    result = events[-1][1]
    assert result.output.is_file() and not runner.running


def test_failures_are_reported_not_raised(dummy, make_cfg, tmp_path):
    runner = JobRunner()
    runner.start(generate_job(make_cfg([dummy.truncated])))                      # no usable input
    kind, (code, message, trace) = wait_for_end(runner)[-1]
    assert (kind, code, trace) == ("failed", 3, None) and "有効な入力" in message

    broken = make_cfg([dummy.base], runtime={"ffmpeg": str(tmp_path / "no-such-ffmpeg")})
    runner.join(5)
    runner.start(generate_job(broken))
    kind, (code, message, _) = wait_for_end(runner)[-1]
    assert (kind, code) == ("failed", 4) and "ffmpeg" in message


def test_a_bug_in_the_job_carries_its_traceback():
    runner = JobRunner()

    def buggy(progress, cancel):
        raise ZeroDivisionError("oops")

    runner.start(buggy)
    kind, (code, message, trace) = wait_for_end(runner)[-1]
    assert kind == "failed" and code == 1 and "ZeroDivisionError: oops" in message and "Traceback" in trace


def test_cancel_stops_a_running_job_quickly(dummy, make_cfg):
    cfg = make_cfg([dummy.base], duration=30, cut={"mode": "fixed", "fixed": 15}, effects={"enabled": ["kaleidoscope"]},
                   video={"width": 1280, "height": 720})
    runner = JobRunner()
    runner.start(generate_job(cfg))
    time.sleep(1.0)
    assert runner.running
    t0 = time.monotonic()
    runner.cancel()
    events = wait_for_end(runner, timeout=20)
    assert events[-1][0] == "cancelled" and time.monotonic() - t0 < 10
    assert not cfg.output.exists()


def test_only_one_job_at_a_time(dummy, make_cfg):
    runner = JobRunner()
    runner.start(generate_job(make_cfg([dummy.base], duration=4)))
    with pytest.raises(RuntimeError, match="実行中"):
        runner.start(generate_job(make_cfg([dummy.base])))
    wait_for_end(runner)
    runner.join(5)
    runner.start(lambda p, c: "second")                         # fine once the first has ended
    assert wait_for_end(runner)[-1] == ("done", "second")


def test_logging_is_captured_only_while_a_job_runs(dummy, make_cfg):
    import logging

    logger = logging.getLogger("moviekaleidoscope")
    before = (len(logger.handlers), logger.level)
    runner = JobRunner()
    runner.start(generate_job(make_cfg([dummy.base], duration=2)))
    wait_for_end(runner)
    runner.join(5)
    assert (len(logger.handlers), logger.level) == before       # handler and level restored


def test_plan_only_and_replay_jobs(dummy, make_cfg, tmp_path):
    runner = JobRunner()
    cfg = make_cfg([dummy.base], duration=3)
    runner.start(generate_job(cfg, plan_only=True))
    result = wait_for_end(runner)[-1][1]
    assert result.output is None and result.plan_path.is_file() and not cfg.output.exists()
    runner.join(5)
    out = tmp_path / "replayed.mp4"
    replay_cfg = make_cfg([], output=str(out))
    runner.start(replay_job(result.plan_path, replay_cfg))
    plan, output = wait_for_end(runner)[-1][1]
    assert output == out and out.is_file() and len(plan.clips) >= 1


# ---- settings model ----------------------------------------------------------------------------------------------
def filled(**kw) -> GuiSettings:
    s = GuiSettings(inputs=[str(ABS / "clips")], output=str(ABS / "out" / "o.mp4"), duration="30")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_defaults_make_a_valid_config():
    cfg = model.to_config(filled())
    assert cfg.cut.mode == "auto" and cfg.effects.enabled == "all" and cfg.effects.exclude == ()
    assert cfg.video.width is None and cfg.audio.file is None and cfg.runtime.jobs is None and cfg.seed is None


def test_every_field_reaches_the_config():
    s = filled(recursive=True, overwrite=True, duration="1:30", audio_file=str(ABS / "m" / "bgm.wav"), audio_start="2.5", bpm="127.5",
               cut_mode="random", cut_fixed="3", cut_random_min="0.5", cut_random_max="2", beats="2, 4", width="1280",
               height="720", fps="29.97", crf="20", preset="slow", seed="42", jobs="3", ffmpeg=str(ABS / "opt" / "ffmpeg"),
               effects={**{n: True for n in model.registry.names()}, "reverse": False, "strobe": False})
    cfg = model.to_config(s)
    assert (cfg.recursive, cfg.overwrite, cfg.duration) == (True, True, 90.0)
    assert (str(cfg.audio.file), cfg.audio.start, cfg.audio.bpm) == (str(ABS / "m" / "bgm.wav"), 2.5, 127.5)
    assert (cfg.cut.mode, cfg.cut.fixed, cfg.cut.random_min, cfg.cut.random_max, cfg.cut.beats) == ("random", 3, 0.5, 2, (2, 4))
    assert (cfg.video.width, cfg.video.height, str(cfg.video.fps), cfg.video.crf, cfg.video.preset) == (1280, 720, "30000/1001", 20, "slow")
    assert (cfg.seed, cfg.runtime.jobs, cfg.runtime.ffmpeg) == (42, 3, str(ABS / "opt" / "ffmpeg"))
    assert cfg.effects.enabled == "all" and set(cfg.effects.exclude) == {"reverse", "strobe"}


@pytest.mark.parametrize("field,value,fragment", [
    ("width", "abc", "幅"), ("height", "7.5", "高さ"), ("crf", "x", "CRF"), ("seed", "1.5", "seed"),
    ("bpm", "fast", "BPM"), ("jobs", "two", "並列数"), ("cut_fixed", "", None), ("beats", "", "拍数"),
    ("beats", "a,b", "拍数"), ("audio_start", "later", "開始位置"), ("duration", "soon", "duration"),
    ("crf", "99", "crf"), ("preset", "fastest", "プリセット"), ("cut_mode", "never", "カット方式"),
    ("output", "", "出力先"), ("inputs", [], "入力"), ("output", "/out/o.mov", ".mp4"),
])
def test_bad_values_give_messages_that_name_the_field(field, value, fragment):
    s = filled(**{field: value})
    if fragment is None:                       # blank numeric fields fall back to their defaults
        model.to_config(s)
        return
    with pytest.raises(ConfigError) as e:
        model.to_config(s)
    assert fragment in str(e.value)


def test_all_effects_off_is_an_error():
    with pytest.raises(ConfigError, match="エフェクト"):
        model.to_config(filled(effects={n: False for n in model.registry.names()}))


def test_blank_fields_do_not_leak_values_from_a_loaded_file(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("inputs: [a]\noutput: o.mp4\nduration: 10\nseed: 7\naudio: {file: x.wav, bpm: 99}\n"
                 "video: {width: 800, height: 600, fps: 24}\n", encoding="utf-8")
    s = model.load_settings_file(f)
    s.seed = s.width = s.height = s.fps = s.audio_file = s.bpm = ""        # the user clears the fields
    cfg = model.to_config(s)
    assert (cfg.seed, cfg.video.width, cfg.video.height, cfg.video.fps, cfg.audio.file, cfg.audio.bpm) == (None,) * 6


def test_settings_survive_save_and_load_including_what_the_form_cannot_show(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump({
        "inputs": ["/a", "/b"], "output": "/o.mp4", "duration": 75, "extensions": ["mp4", "mov"],
        "audio": {"file": "/m.wav", "start": 3, "beat_offset": 0.4, "bpm_range": [80, 160]},
        "cut": {"mode": "beat", "beats": [2, 4]},
        "effects": {"enabled": ["hue", "flip", "tile"], "exclude": ["tile"], "weights": {"hue": 3},
                    "params": {"reverse": {"max_sec": 6}}},
        "runtime": {"keep_temp": True, "work_dir": "/w"}}), encoding="utf-8")
    s = model.load_settings_file(f)
    assert s.effects["hue"] and s.effects["flip"] and not s.effects["tile"] and not s.effects["zoom"]
    out = tmp_path / "saved.yaml"
    model.save_settings_file(s, out)
    original, saved = model.build_config(f, {}, tmp_path), model.build_config(out, {}, tmp_path)
    # the unticked effects are now expressed as an exclusion (not an allow-list): same effects, new shape
    assert saved.effects.exclude and set(saved.effects.exclude) >= {"tile", "zoom"}
    saved_dict, original_dict = saved.to_file_dict(), original.to_file_dict()
    for key in ("extensions", "audio", "runtime", "inputs", "output", "duration", "cut"):
        assert saved_dict[key] == original_dict[key], key
    assert saved.effects.weights == {"hue": 3.0} and saved.effects.params == {"reverse": {"max_sec": 6}}


def test_remember_and_recall(tmp_path):
    s = filled(cut_mode="beat", overwrite=True, seed="9")
    model.remember(s, tmp_path)
    back = model.recall(tmp_path)
    assert (back.cut_mode, back.seed, back.inputs, back.output) == ("beat", "9", [str(ABS / "clips")], str(ABS / "out" / "o.mp4"))
    assert back.overwrite is False                     # never silently overwrite after a restart
    assert model.recall(tmp_path / "nothing-here").inputs == []
    (tmp_path / "last.yaml").write_text("{ not: [valid", encoding="utf-8")
    assert model.recall(tmp_path) == GuiSettings()     # a damaged file is ignored, not fatal
