from __future__ import annotations

import wave
from fractions import Fraction

import pytest

from moviekaleidoscope.cli import main
from moviekaleidoscope.errors import NoValidInputError, ToolNotFoundError
from moviekaleidoscope.media.inputs import collect_input_paths, validate_inputs
from moviekaleidoscope.media.probe import ProbeError, probe_video
from moviekaleidoscope.media.tools import locate_tools, parse_version

pytestmark = pytest.mark.integration


# ---- tools ------------------------------------------------------------------
def test_locate_tools(tools):
    assert tools.ffmpeg and tools.ffprobe
    assert tools.version is None or tools.version >= (4, 4)


def test_locate_tools_explicit_missing_is_error(tmp_path):
    with pytest.raises(ToolNotFoundError, match="ffmpeg"):
        locate_tools(ffmpeg=str(tmp_path / "no-such-ffmpeg"))


def test_locate_tools_env_var(monkeypatch, tools):
    monkeypatch.setenv("FFMPEG_BIN", tools.ffmpeg)
    assert locate_tools().ffmpeg == tools.ffmpeg
    monkeypatch.setenv("FFMPEG_BIN", "definitely-not-installed-xyz")
    with pytest.raises(ToolNotFoundError, match="FFMPEG_BIN"):
        locate_tools()


@pytest.mark.parametrize("text,expected", [
    ("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright", (4, 4)),
    ("ffmpeg version 6.1.1-essentials_build", (6, 1)),
    ("ffmpeg version n7.0 Copyright", (7, 0)),
    ("ffmpeg version N-113456-gabcdef Copyright", None),
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


# ---- probe ------------------------------------------------------------------
def test_probe_valid(tools, dummy):
    info = probe_video(dummy.base, tools)
    assert (info.width, info.height, info.fps) == (1280, 720, Fraction(30))
    assert info.duration == pytest.approx(10, abs=0.1)


def test_probe_other_shapes(tools, dummy):
    assert (probe_video(dummy.vertical, tools).width, probe_video(dummy.vertical, tools).height) == (1080, 1920)
    assert probe_video(dummy.small43, tools).fps == Fraction(24)
    assert probe_video(dummy.ntsc, tools).fps == Fraction(30000, 1001)


def test_probe_applies_rotation(tools, dummy):
    info = probe_video(dummy.rotated, tools)
    assert (info.width, info.height) == (240, 320)


def test_probe_unicode_path(tools, dummy):
    assert probe_video(dummy.japanese, tools).width == 320


@pytest.mark.parametrize("attr", ["truncated", "empty", "not_video"])
def test_probe_broken_files(tools, dummy, attr):
    with pytest.raises(ProbeError):
        probe_video(getattr(dummy, attr), tools)


def test_probe_audio_only_has_no_video_stream(tools, dummy):
    with pytest.raises(ProbeError, match="映像"):
        probe_video(dummy.click_wav, tools)


# ---- validation ---------------------------------------------------------------
def test_validate_skips_bad_inputs_and_keeps_order(tools, dummy):
    paths = [dummy.base, dummy.truncated, dummy.small43, dummy.empty, dummy.tiny,
             dummy.not_video, dummy.short, dummy.click_wav]
    report = validate_inputs(paths, tools)
    assert [i.path for i in report.valid] == [dummy.base, dummy.small43, dummy.short]
    assert {p for p, _ in report.skipped} == {dummy.truncated, dummy.empty, dummy.tiny,
                                              dummy.not_video, dummy.click_wav}
    assert any("短すぎ" in why for p, why in report.skipped if p == dummy.tiny)


def test_validate_reports_progress(tools, dummy):
    calls = []
    validate_inputs([dummy.base, dummy.short], tools, lambda s, c, t: calls.append((s, c, t)))
    assert calls[0] == ("入力検証", 0, 2) and calls[-1] == ("入力検証", 2, 2)


def test_validate_no_valid_input(tools, dummy):
    with pytest.raises(NoValidInputError):
        validate_inputs([dummy.truncated, dummy.empty], tools)


def test_folder_with_japanese_names(tools, dummy):
    paths = collect_input_paths([dummy.japanese.parent], False, ["mp4"])
    assert [i.path for i in validate_inputs(paths, tools).valid] == [dummy.japanese]


# ---- dummy click track ---------------------------------------------------------
def test_click_wav_shape(dummy):
    with wave.open(str(dummy.click_wav)) as w:
        assert w.getframerate() == 22050
        assert w.getnframes() / w.getframerate() == pytest.approx(30, abs=0.01)


# ---- CLI (M0 scope) --------------------------------------------------------------
def test_cli_dump_config(capsys):
    assert main(["--dump-config", "--cut-length", "3", "--fps", "29.97"]) == 0
    out = capsys.readouterr().out
    assert "mode: fixed" in out and "fps: 30000/1001" in out


def test_cli_exit_codes(dummy, tmp_path):
    assert main([]) == 2                                                       # no inputs
    assert main(["-i", str(tmp_path / "missing.mp4"), "-o", "o.mp4", "-d", "5"]) == 2
    assert main(["-i", str(dummy.truncated), "-o", str(tmp_path / "o.mp4"), "-d", "5"]) == 3
    assert main(["-i", str(dummy.base), "-o", str(tmp_path / "o.mp4"), "-d", "5",
                 "--ffmpeg", str(tmp_path / "nope")]) == 4
