"""Music: BPM analysis on a real file, cutting on the beat, and audio/video sync of the output."""

from __future__ import annotations

import json
import logging
import subprocess

import numpy as np
import pytest
from tools.make_dummy import beat_signal, make_solid_video, write_wav

from moviekaleidoscope.api import generate, load_plan, render_plan
from moviekaleidoscope.audio.bpm import analyze_audio
from moviekaleidoscope.cli import main
from moviekaleidoscope.errors import AnalysisError

cv2 = pytest.importorskip("cv2")
pytestmark = pytest.mark.integration
FPS = 30


@pytest.fixture(scope="module")
def colours(tools, tmp_path_factory):
    d = tmp_path_factory.mktemp("solid")
    return [make_solid_video(tools, d / f"{c}.mp4", c) for c in ("red", "green", "blue", "yellow")]


@pytest.fixture(scope="module")
def wavs(tmp_path_factory):
    d = tmp_path_factory.mktemp("wavs")
    return {
        "short": write_wav(d / "short_4s.wav", beat_signal(120, 0.25, 4)),
        "noise": write_wav(d / "noise.wav", (np.random.RandomState(1).randn(22050 * 20) * 0.1).astype(np.float32)),
        "house": write_wav(d / "house_126.wav", beat_signal(126, 0.4, 40, kind="house")),
    }


def audio_samples(tools, path, sr=22050) -> np.ndarray:
    raw = subprocess.run([tools.ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
                          "-f", "f32le", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<f4")


def click_times(x: np.ndarray, sr=22050, refractory=0.25) -> list[float]:
    """Onset times of the clicks (first sample above 30 % of the peak after a quiet spell)."""
    hot = np.flatnonzero(np.abs(x) > 0.3 * np.abs(x).max())
    times, last = [], -1e9
    for i in hot:
        if i / sr - last > refractory:
            times.append(i / sr)
        last = i / sr
    return times


def cut_times(path) -> list[float]:
    """Times (s) of frames whose flat colour differs from the previous frame's."""
    cap = cv2.VideoCapture(str(path))
    prev, out, k = None, [], 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        m = f.reshape(-1, 3).mean(axis=0)
        if prev is not None and np.abs(m - prev).max() > 40:
            out.append(k / FPS)
        prev, k = m, k + 1
    return out


def streams(ffprobe_json, path):
    info = ffprobe_json(path)
    return ({s["codec_type"]: s for s in info["streams"]}, float(info["format"]["duration"]))


# ---- analysis of a real file ---------------------------------------------------------------------------------------
def test_analysis_of_a_real_audio_file(tools, dummy):
    g = analyze_audio(tools, dummy.click_wav, 0.0, 30.0, (70.0, 180.0))
    assert g.bpm == pytest.approx(120.0, abs=0.01) and g.offset == pytest.approx(0.25, abs=0.005)


def test_analysis_starts_at_audio_start(tools, wavs):
    g = analyze_audio(tools, wavs["house"], 1.0, 30.0, (70.0, 180.0))       # beats at 0.4 + k*0.476..; from t=1.0
    period = 60 / 126
    expected = (0.4 - 1.0) % period
    assert g.bpm == pytest.approx(126, abs=0.01) and abs(((g.offset - expected + period / 2) % period) - period / 2) < 0.01


def test_undecodable_audio_is_an_analysis_error(tools, dummy):
    with pytest.raises(AnalysisError):
        analyze_audio(tools, dummy.not_video, 0.0, 10.0, (70.0, 180.0))


# ---- the main promise: cuts land on the beat, audio and video stay in sync ----------------------------------------------
def test_cuts_land_on_the_clicks(colours, dummy, make_cfg, ffprobe_json, tools):
    cfg = make_cfg(colours, duration=10, cut={"mode": "auto", "beats": [1]}, effects={"enabled": ["flip"]},
                   audio={"file": str(dummy.click_wav)}, seed=3)
    result = generate(cfg)
    plan = result.plan
    assert plan.cut_mode == "beat" and plan.audio.bpm == pytest.approx(120, abs=0.01)
    assert plan.audio.beat_offset == pytest.approx(0.25, abs=0.005)

    st, dur = streams(ffprobe_json, result.output)
    assert st["audio"]["codec_name"] == "aac" and abs(float(st["audio"]["start_time"])) < 0.05
    assert abs(float(st["video"]["start_time"])) < 0.01
    assert dur == pytest.approx(10.0, abs=0.06)
    assert int(st["video"]["nb_frames"]) == 300

    clicks = click_times(audio_samples(tools, result.output))
    assert clicks[0] == pytest.approx(0.25, abs=0.03)                          # music starts where it should
    cuts = cut_times(result.output)
    boundaries = [c.start_frame / FPS for c in plan.clips[1:]]
    assert len(cuts) >= 0.6 * len(boundaries)                                  # (same colour twice in a row: no visible cut)
    assert cuts[0] == pytest.approx(0.25, abs=0.04)                            # lead-in ends on the first beat
    for c in cuts:
        assert min(abs(c - k) for k in clicks) < 0.04, f"cut at {c:.3f}s is not on a click"


def test_audio_start_moves_the_music_and_the_grid(colours, dummy, make_cfg, ffprobe_json, tools):
    cfg = make_cfg(colours, duration=6, cut={"mode": "auto", "beats": [1]}, effects={"enabled": ["flip"]},
                   audio={"file": str(dummy.click_wav), "start": 1.0}, seed=4)
    result = generate(cfg)
    assert result.plan.audio.start == 1.0 and result.plan.audio.beat_offset == pytest.approx(0.25, abs=0.005)
    clicks = click_times(audio_samples(tools, result.output))
    assert clicks[0] == pytest.approx(0.25, abs=0.03)                          # source click at 1.25 s -> 0.25 s
    assert all(min(abs(c - k) for k in clicks) < 0.04 for c in cut_times(result.output))


def test_house_track_cuts_on_the_kick(colours, wavs, make_cfg, tools):
    """A pattern with off-beat hats: the cuts must go on the kicks (not half a beat away)."""
    cfg = make_cfg(colours, duration=12, cut={"mode": "auto", "beats": [1, 2]}, effects={"enabled": ["flip"]},
                   audio={"file": str(wavs["house"])}, seed=5)
    result = generate(cfg)
    # a 12 s video is analysed over 12 s: the tempo is good to ~0.03 BPM (< 3 ms of drift in the clip)
    assert result.plan.audio.bpm == pytest.approx(126, abs=0.1)
    period = 60 / 126
    for c in result.plan.clips[1:]:
        t = c.start_frame / FPS
        phase = ((t - 0.4 + period / 2) % period) - period / 2
        assert abs(phase) < 1 / FPS, f"cut at {t:.3f}s is {phase * 1000:.0f} ms from a kick"


# ---- length rules ------------------------------------------------------------------------------------------------------------
def test_duration_defaults_to_the_music_length(dummy, wavs, make_cfg, ffprobe_json):
    for start, frames in ((0.0, 120), (0.5, 105)):
        cfg = make_cfg([dummy.base], duration=None, cut={"mode": "fixed", "fixed": 1.0},
                       audio={"file": str(wavs["short"]), "start": start}, overwrite=True)
        result = generate(cfg)
        assert result.plan.output.frames == frames
        st, dur = streams(ffprobe_json, result.output)
        assert int(st["video"]["nb_frames"]) == frames and dur == pytest.approx(frames / FPS, abs=0.06)


def test_short_music_is_padded_with_silence(dummy, wavs, make_cfg, ffprobe_json, tools, caplog):
    cfg = make_cfg([dummy.base], duration=8, cut={"mode": "fixed", "fixed": 2}, audio={"file": str(wavs["short"])})
    with caplog.at_level(logging.WARNING):
        result = generate(cfg)
    assert "4.0 秒短い" in caplog.text
    st, dur = streams(ffprobe_json, result.output)
    assert dur == pytest.approx(8.0, abs=0.06) and "audio" in st
    x = audio_samples(tools, result.output)
    assert np.abs(x[: 22050 * 3]).max() > 0.2 and np.abs(x[int(22050 * 4.3):]).max() < 0.01


def test_long_music_is_cut_to_the_video(dummy, make_cfg, ffprobe_json):
    cfg = make_cfg([dummy.base], duration=3, cut={"mode": "fixed", "fixed": 1}, audio={"file": str(dummy.click_wav)})
    _, dur = streams(ffprobe_json, generate(cfg).output)
    assert dur == pytest.approx(3.0, abs=0.06)


# ---- bad or unusable music ---------------------------------------------------------------------------------------------------------
def test_unusable_audio_files_are_clear_errors(dummy, tmp_path, caplog):
    base = ["-i", str(dummy.base), "-o", str(tmp_path / "o.mp4"), "-d", "3", "-q"]
    assert main([*base, "--audio", str(tmp_path / "missing.wav")]) == 2
    assert main([*base, "--audio", str(dummy.base)]) == 2                   # a video without an audio stream
    assert main([*base, "--audio", str(dummy.not_video)]) == 2
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.mp4.plan.json").exists()


def test_audio_start_beyond_the_end_is_an_error(dummy, wavs, tmp_path):
    rc = main(["-i", str(dummy.base), "-o", str(tmp_path / "o.mp4"), "--audio", str(wavs["short"]), "-q"])
    assert rc == 0
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(f"audio: {{file: '{wavs['short']}', start: 9}}\n", encoding="utf-8")
    assert main(["-c", str(cfg_file), "-i", str(dummy.base), "-o", str(tmp_path / "o2.mp4"), "-q"]) == 2


def test_unanalysable_music_falls_back_to_fixed_cuts_unless_beat_is_demanded(dummy, wavs, make_cfg, caplog, tmp_path):
    auto = make_cfg([dummy.base], duration=6, cut={"mode": "auto", "fixed": 2}, audio={"file": str(wavs["noise"])})
    with caplog.at_level(logging.WARNING):
        result = generate(auto)
    assert result.plan.cut_mode == "fixed" and "固定長" in caplog.text and result.plan.audio.bpm is None
    assert {c.frames for c in result.plan.clips} == {60}

    out = tmp_path / "strict.mp4"
    rc = main(["-i", str(dummy.base), "-o", str(out), "-d", "4", "--audio", str(wavs["noise"]), "--cut-mode", "beat", "-q"])
    assert rc == 1 and not out.exists()                                     # explicit beat: an error, with --bpm as the way out
    rc = main(["-i", str(dummy.base), "-o", str(out), "-d", "4", "--audio", str(wavs["noise"]), "--cut-mode", "beat",
               "--bpm", "100", "-q"])
    assert rc == 0 and out.is_file()


# ---- BPM without music, and BPM with cuts that ignore it -----------------------------------------------------------------------
def test_manual_bpm_without_music_gives_a_silent_video_cut_on_the_beat(colours, make_cfg, ffprobe_json):
    cfg = make_cfg(colours, duration=6, cut={"mode": "auto", "beats": [1]}, effects={"enabled": ["flip"]},
                   audio={"bpm": 90, "beat_offset": 0.1}, seed=2)
    result = generate(cfg)
    assert result.plan.audio.path is None and result.plan.cut_mode == "beat"
    st, _ = streams(ffprobe_json, result.output)
    assert "audio" not in st
    period = 60 / 90
    for c in cut_times(result.output):
        assert abs(((c - 0.1 + period / 2) % period) - period / 2) < 1 / FPS


def test_random_cuts_ignore_the_music_but_effects_can_still_use_its_bpm(dummy, make_cfg, ffprobe_json):
    cfg = make_cfg([dummy.base], duration=6, cut={"mode": "random"}, audio={"file": str(dummy.click_wav), "bpm": 100})
    result = generate(cfg)
    assert result.plan.cut_mode == "random" and result.plan.audio.bpm == 100
    st, _ = streams(ffprobe_json, result.output)
    assert "audio" in st and len({c.frames for c in result.plan.clips}) > 1


# ---- plan replay keeps the music ----------------------------------------------------------------------------------------------------
def test_render_from_plan_includes_the_music(dummy, make_cfg, ffprobe_json, tools, tmp_path):
    cfg = make_cfg([dummy.base], duration=4, cut={"mode": "auto"}, audio={"file": str(dummy.click_wav)})
    planned = generate(cfg, plan_only=True)
    saved = json.loads(planned.plan_path.read_text(encoding="utf-8"))
    assert saved["audio"]["path"].endswith("click_120bpm.wav") and saved["cut_mode"] == "beat"
    out = tmp_path / "replay.mp4"
    render_plan(load_plan(planned.plan_path), cfg, out, tools)
    st, dur = streams(ffprobe_json, out)
    assert "audio" in st and dur == pytest.approx(4.0, abs=0.06)
    assert main(["--from-plan", str(planned.plan_path), "-o", str(tmp_path / "replay2.mp4"), "-q"]) == 0


def test_cli_end_to_end_with_music(dummy, tmp_path, ffprobe_json, caplog):
    out = tmp_path / "cli.mp4"
    caplog.set_level(logging.INFO)
    rc = main(["-i", str(dummy.base), "-i", str(dummy.small43), "-o", str(out), "-d", "6",
               "--audio", str(dummy.click_wav), "--beats", "2,4", "--seed", "8", "--jobs", "2", "-q"])
    assert rc == 0
    st, dur = streams(ffprobe_json, out)
    assert "audio" in st and dur == pytest.approx(6.0, abs=0.06)
    plan = json.loads((tmp_path / "cli.mp4.plan.json").read_text(encoding="utf-8"))
    assert plan["cut_mode"] == "beat" and plan["audio"]["bpm"] == pytest.approx(120, abs=0.01)
