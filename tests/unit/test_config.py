from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from moviekaleidoscope.cli import build_parser, overrides_from_args
from moviekaleidoscope.config import (
    Config, build_config, config_from_dict, parse_duration, parse_fps, validate_for_run,
)
from moviekaleidoscope.errors import ConfigError

REPO = Path(__file__).resolve().parents[2]


def cli(*argv: str) -> dict:
    return overrides_from_args(build_parser().parse_args(list(argv)))


def test_defaults():
    cfg = config_from_dict({})
    assert cfg.cut.mode == "auto" and cfg.cut.fixed == 2.0 and cfg.cut.beats == (4,)
    assert cfg.video.crf == 18 and cfg.video.preset == "medium"
    assert cfg.runtime.jobs is None and cfg.audio.file is None


def test_example_config_loads():
    cfg = build_config(REPO / "config.example.yaml", {}, Path("/nowhere"))
    assert cfg.duration == 60
    assert cfg.inputs[0] == REPO / "clips"  # relative to the config file, not cwd
    assert cfg.output == REPO / "out" / "result.mp4"


def test_unknown_keys_are_errors():
    with pytest.raises(ConfigError, match="foo"):
        config_from_dict({"foo": 1})
    with pytest.raises(ConfigError, match="cut.moed"):
        config_from_dict({"cut": {"moed": "fixed"}})


def test_cli_beats_file_and_lists_replace(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("inputs: [a.mp4, b.mp4]\ncut: {mode: fixed, fixed: 3}\nvideo: {crf: 20}\n", encoding="utf-8")
    cfg = build_config(f, cli("-i", "x.mp4", "--cut-length", "5"), tmp_path / "cwd")
    assert cfg.inputs == (tmp_path / "cwd" / "x.mp4",)      # replaced, CLI relative to cwd
    assert cfg.cut.fixed == 5 and cfg.video.crf == 20       # CLI wins, other file values kept


def test_file_relative_paths_use_config_dir(tmp_path):
    (tmp_path / "conf").mkdir()
    f = tmp_path / "conf" / "c.yaml"
    f.write_text("inputs: ../clips\noutput: out.mp4\naudio: {file: bgm.wav}\n", encoding="utf-8")
    cfg = build_config(f, {}, tmp_path / "elsewhere")
    assert cfg.inputs == (tmp_path / "conf" / ".." / "clips",)
    assert cfg.output == tmp_path / "conf" / "out.mp4"
    assert cfg.audio.file == tmp_path / "conf" / "bgm.wav"


def test_no_audio_overrides_file(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("audio: {file: bgm.wav}\n", encoding="utf-8")
    assert build_config(f, cli("--no-audio"), tmp_path).audio.file is None


@pytest.mark.parametrize("bad", [
    {"video": {"crf": 99}}, {"video": {"preset": "fastest"}}, {"cut": {"mode": "x"}},
    {"cut": {"beats": []}}, {"cut": {"beats": [0]}}, {"cut": {"fixed": 0}},
    {"cut": {"random": {"min": 5, "max": 1}}}, {"audio": {"bpm_range": [180, 70]}},
    {"audio": {"bpm": -1}}, {"duration": 0}, {"seed": -1}, {"recursive": "yes"},
    {"runtime": {"jobs": 0}}, {"video": {"width": 0}}, {"inputs": 5},
])
def test_validation_errors(bad):
    with pytest.raises(ConfigError):
        config_from_dict(bad)


def test_jobs_auto():
    assert config_from_dict({"runtime": {"jobs": "auto"}}).runtime.jobs is None
    assert config_from_dict({"runtime": {"jobs": 3}}).runtime.jobs == 3


@pytest.mark.parametrize("value,expected", [
    (60, 60.0), ("90", 90.0), ("1:30", 90.0), ("1:00:00", 3600.0), ("0:01.5", 1.5), (2.5, 2.5),
])
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "-5", 0, -1, True, "1:-5"])
def test_parse_duration_errors(bad):
    with pytest.raises(ConfigError):
        parse_duration(bad)


@pytest.mark.parametrize("value,expected", [
    (30, Fraction(30)), ("30000/1001", Fraction(30000, 1001)), (29.97, Fraction(30000, 1001)),
    ("29.97", Fraction(30000, 1001)), (23.976, Fraction(24000, 1001)), (59.94, Fraction(60000, 1001)),
    (24, Fraction(24)), (25.5, Fraction(51, 2)),
])
def test_parse_fps(value, expected):
    assert parse_fps(value) == expected


@pytest.mark.parametrize("bad", [0, -3, "x", "1/0", 500, True])
def test_parse_fps_errors(bad):
    with pytest.raises(ConfigError):
        parse_fps(bad)


# ---- CLI cut-option rules (SPEC §10) --------------------------------------
def test_cut_options_imply_mode():
    assert cli("--cut-length", "3")["cut"] == {"mode": "fixed", "fixed": 3.0}
    assert cli("--cut-random", "1:4")["cut"] == {"mode": "random", "random": {"min": 1.0, "max": 4.0}}
    assert cli("--beats", "2,4")["cut"] == {"mode": "beat", "beats": [2, 4]}
    assert cli("--cut-mode", "fixed", "--cut-length", "3")["cut"]["mode"] == "fixed"


@pytest.mark.parametrize("argv", [
    ["--cut-length", "3", "--beats", "4"],
    ["--cut-length", "3", "--cut-random", "1:2"],
    ["--cut-mode", "beat", "--cut-length", "3"],
    ["--cut-random", "abc"],
    ["--beats", "a,b"],
])
def test_cut_option_conflicts(argv):
    with pytest.raises(ConfigError):
        cli(*argv)


# ---- validate_for_run -------------------------------------------------------
def _runnable(**kw) -> Config:
    base = {"inputs": ["a.mp4"], "output": "o.mp4", "duration": 10}
    return config_from_dict({**base, **kw})


def test_validate_for_run_ok(tmp_path):
    validate_for_run(config_from_dict({"inputs": [str(tmp_path / "a.mp4")],
                                       "output": str(tmp_path / "o.mp4"), "duration": 5}))


@pytest.mark.parametrize("kw,msg", [
    ({"inputs": []}, "入力"),
    ({"output": None}, "出力先"),
    ({"output": "o.mov"}, ".mp4"),
    ({"duration": None}, "長さ"),
])
def test_validate_for_run_errors(kw, msg):
    with pytest.raises(ConfigError, match=msg):
        validate_for_run(_runnable(**kw))


def test_validate_for_run_audio_supplies_duration():
    validate_for_run(_runnable(duration=None, audio={"file": "bgm.wav"}))


def test_validate_for_run_existing_output_needs_overwrite(tmp_path):
    out = tmp_path / "o.mp4"
    out.write_bytes(b"x")
    cfg = _runnable(output=str(out))
    with pytest.raises(ConfigError, match="-y"):
        validate_for_run(cfg)
    cfg.overwrite = True
    validate_for_run(cfg)
    (tmp_path / "o.mp4.plan.json").write_text("{}")
    out.unlink()
    cfg.overwrite = False
    with pytest.raises(ConfigError, match="plan"):
        validate_for_run(cfg)


def test_effects_exclude_and_cli_options():
    cfg = config_from_dict({"effects": {"exclude": ["reverse", "speed"]}})
    assert cfg.effects.exclude == ("reverse", "speed")
    assert config_from_dict({"effects": {"exclude": []}}).effects.exclude == ()
    assert cli("--effects", "hue,flip")["effects"] == {"enabled": ["hue", "flip"]}
    assert cli("--exclude-effects", "reverse")["effects"] == {"exclude": ["reverse"]}
    with pytest.raises(ConfigError):
        config_from_dict({"effects": {"exclude": 5}})


# ---- saving a config: the file shape round-trips -----------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    {},
    {"inputs": ["a.mp4", "/abs/dir"], "output": "o.mp4", "duration": 90, "seed": 5, "overwrite": True, "recursive": True},
    {"video": {"width": 1280, "height": 720, "fps": "30000/1001", "crf": 20, "preset": "slow"}},
    {"audio": {"file": "bgm.wav", "start": 2.5, "bpm": 128.4, "beat_offset": 0.2, "bpm_range": [80, 160]}},
    {"cut": {"mode": "random", "random": {"min": 0.5, "max": 3}, "beats": [1, 2, 4], "fixed": 1.25}},
    {"effects": {"enabled": ["hue", "flip"], "exclude": ["flip"], "weights": {"hue": 3.0},
                 "params": {"rgbshift": {"amount": {"min": 2, "max": 9}}, "reverse": {"max_sec": 6}}}},
    {"runtime": {"jobs": 3, "work_dir": "/tmp/w", "keep_temp": True, "ffmpeg": "/usr/bin/ffmpeg"}},
    {"extensions": ["mp4", "mov"], "plan": "p.json"},
])
def test_file_dict_round_trips(raw):
    cfg = config_from_dict(raw)
    again = config_from_dict(cfg.to_file_dict())
    assert again == cfg
    assert yaml_roundtrip(cfg.to_file_dict()) == cfg.to_file_dict()          # and survives being written as YAML


def yaml_roundtrip(d):
    import yaml
    return yaml.safe_load(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))


def test_dump_config_output_is_a_loadable_config_file(tmp_path, capsys):
    from moviekaleidoscope.cli import main

    assert main(["--dump-config", "--cut-random", "1:3", "--fps", "29.97", "--effects", "hue,flip",
                 "-i", "x.mp4", "-o", "out.mp4", "-d", "1:30", "--seed", "4"]) == 0
    dumped = capsys.readouterr().out
    f = tmp_path / "dumped.yaml"
    f.write_text(dumped, encoding="utf-8")
    assert main(["-c", str(f), "--dump-config"]) == 0              # it loads (this used to fail: unknown key cut.random_min)
    assert capsys.readouterr().out == dumped                        # and says the same again
