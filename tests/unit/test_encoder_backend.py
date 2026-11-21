"""resolve_encoder_backend() and encoder_args(backend="nvenc") (GPU redesign SPEC §A)."""

from __future__ import annotations

import pytest

from moviekaleidoscope.media.tools import Tools
from moviekaleidoscope.render import pipeline
from moviekaleidoscope.render.ffmpeg import EncodeSettings, encoder_args


def make_tools(ffmpeg: str = "ffmpeg") -> Tools:
    return Tools(ffmpeg=ffmpeg, ffprobe="ffprobe", version=(9, 1), version_text="ffmpeg version 9.1")


@pytest.fixture(autouse=True)
def clear_cache():
    pipeline._backend_cache.clear()
    yield
    pipeline._backend_cache.clear()


def test_explicit_backend_is_never_probed(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: calls.append(a) or (0, "", ""))
    assert pipeline.resolve_encoder_backend(make_tools(), "software") == "software"
    assert pipeline.resolve_encoder_backend(make_tools(), "nvenc") == "nvenc"
    assert calls == []


def test_auto_falls_back_when_encoder_is_not_listed(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "libx264 ...\n", ""))
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "software"


def test_auto_falls_back_when_the_probe_encode_fails(monkeypatch):
    calls = iter([(0, "h264_nvenc ...\n", ""), (1, "", "no CUDA device")])
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: next(calls))
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "software"


def test_auto_resolves_to_nvenc_when_listed_and_the_probe_succeeds(monkeypatch):
    calls = iter([(0, "h264_nvenc ...\n", ""), (0, "", "")])
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: next(calls))
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "nvenc"


def test_resolution_is_cached_per_ffmpeg_path(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(a)
        # odd calls = the "-encoders" check, even calls = the test-encode probe
        return (0, "h264_nvenc\n", "") if len(calls) % 2 == 1 else (0, "", "")

    monkeypatch.setattr(pipeline, "run_capture", fake)
    t = make_tools()
    assert pipeline.resolve_encoder_backend(t, "auto") == "nvenc"
    assert pipeline.resolve_encoder_backend(t, "auto") == "nvenc"
    assert len(calls) == 2     # probed once (two run_capture calls inside it), not twice

    assert pipeline.resolve_encoder_backend(make_tools("other-ffmpeg"), "auto") == "nvenc"
    assert len(calls) == 4     # a different ffmpeg path is probed again


def test_encoder_args_nvenc_uses_hyphenated_aq_options():
    args = encoder_args(EncodeSettings(19, "medium", "nvenc"))
    assert args[:2] == ["-c:v", "h264_nvenc"]
    assert args[args.index("-cq") + 1] == "19"
    assert "-spatial-aq" in args and "-temporal-aq" in args
    # the underscore spellings were removed in ffmpeg 9.0+ ("Remove deprecated NVENC options...")
    assert "-spatial_aq" not in args and "-temporal_aq" not in args


def test_encoder_args_software_is_unchanged():
    args = encoder_args(EncodeSettings(18, "medium", "software"))
    assert args[:2] == ["-c:v", "libx264"]
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "medium"
