"""resolve_encoder_backend(), encoder_args(backend="nvenc") and the NVENC concurrent-session
backoff (GPU redesign SPEC §A)."""

from __future__ import annotations

import subprocess

import pytest

from moviekaleidoscope.errors import RenderError
from moviekaleidoscope.media.tools import Tools
from moviekaleidoscope.render import pipeline
from moviekaleidoscope.render.ffmpeg import EncodeSettings, encoder_args
from moviekaleidoscope.render.pipeline import (
    _NvencGate,
    _looks_like_nvenc_capacity_error,
    _render_with_nvenc_backoff,
)


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


def test_auto_falls_back_when_listing_encoders_raises(monkeypatch):
    """A hung/missing ffmpeg must fall back, not propagate (never abort the render)."""
    def raises(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)
    monkeypatch.setattr(pipeline, "run_capture", raises)
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "software"


def test_auto_falls_back_when_the_nvenc_timing_fails(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: None if codec == "h264_nvenc" else 1.0)
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "software"


def test_auto_resolves_to_nvenc_when_it_is_faster(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    times = {"h264_nvenc": 0.2, "libx264": 0.5}
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: times[codec])
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "nvenc"


def test_auto_resolves_to_software_when_nvenc_is_dramatically_slower(monkeypatch):
    """A truly broken/crippled GPU (not just "a bit slower") must not regress vs. software-only."""
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    times = {"h264_nvenc": 2.0, "libx264": 0.3}   # ~6.7x slower, well past the tolerance
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: times[codec])
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "software"


def test_auto_still_prefers_nvenc_when_only_moderately_slower_in_this_lone_probe(monkeypatch):
    """Mirrors what was actually measured on real hardware (2026-11-22): nvenc's real advantage
    (relieving CPU contention under many concurrent jobs) is invisible to a single-stream probe,
    which measured nvenc as ~1.7x slower there even though a real multi-clip render was ~15%
    faster with nvenc. A moderate single-stream slowdown is therefore tolerated, not penalised."""
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    times = {"h264_nvenc": 0.27, "libx264": 0.16}
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: times[codec])
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "nvenc"


def test_auto_resolves_to_nvenc_right_at_the_slowdown_threshold(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    times = {"h264_nvenc": 1.0, "libx264": 0.5}   # exactly _NVENC_MAX_SLOWDOWN (2x) -> still nvenc
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: times[codec])
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "nvenc"


def test_auto_resolves_to_software_just_past_the_slowdown_threshold(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    times = {"h264_nvenc": 1.01, "libx264": 0.5}  # just over 2x -> software
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: times[codec])
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "software"


def test_auto_resolves_to_nvenc_when_the_software_timing_itself_fails(monkeypatch):
    """nvenc did demonstrably work; a failed comparison is not a reason to distrust it."""
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "h264_nvenc ...\n", ""))
    monkeypatch.setattr(pipeline, "_time_encode", lambda tools, codec: 1.0 if codec == "h264_nvenc" else None)
    assert pipeline.resolve_encoder_backend(make_tools(), "auto") == "nvenc"


def test_time_encode_returns_none_when_run_capture_raises(monkeypatch):
    def raises(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)
    monkeypatch.setattr(pipeline, "run_capture", raises)
    assert pipeline._time_encode(make_tools(), "h264_nvenc") is None


def test_time_encode_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (1, "", "error"))
    assert pipeline._time_encode(make_tools(), "h264_nvenc") is None


def test_time_encode_returns_elapsed_seconds_on_success(monkeypatch):
    monkeypatch.setattr(pipeline, "run_capture", lambda *a, **k: (0, "", ""))
    elapsed = pipeline._time_encode(make_tools(), "h264_nvenc")
    assert elapsed is not None and elapsed >= 0


def test_resolution_is_cached_per_ffmpeg_path(monkeypatch):
    list_calls = []
    time_calls = []
    monkeypatch.setattr(pipeline, "run_capture",
                        lambda *a, **k: list_calls.append(a) or (0, "h264_nvenc\n", ""))
    monkeypatch.setattr(pipeline, "_time_encode",
                        lambda tools, codec: time_calls.append(codec) or {"h264_nvenc": 0.2, "libx264": 0.5}[codec])
    t = make_tools()
    assert pipeline.resolve_encoder_backend(t, "auto") == "nvenc"
    assert pipeline.resolve_encoder_backend(t, "auto") == "nvenc"
    assert len(list_calls) == 1     # "-encoders" listed once
    assert len(time_calls) == 2     # nvenc + software timed once, not twice

    assert pipeline.resolve_encoder_backend(make_tools("other-ffmpeg"), "auto") == "nvenc"
    assert len(list_calls) == 2     # a different ffmpeg path is probed again
    assert len(time_calls) == 4


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


_NVENC_CAPACITY_MESSAGE = (
    "クリップ 6: エンコーダが異常終了しました:\n"
    "[vf#0:0 @ 0x1] Task finished with error: Generic error in an external library\n"
    "[vost#0:0/h264_nvenc @ 0x2] Could not open encoder before EOF"
)


def test_looks_like_nvenc_capacity_error_matches_the_observed_message():
    assert _looks_like_nvenc_capacity_error(_NVENC_CAPACITY_MESSAGE)


def test_looks_like_nvenc_capacity_error_ignores_unrelated_failures():
    assert not _looks_like_nvenc_capacity_error("クリップ 1: フレーム数が一致しません(期待 10、実際 9)")


def test_gate_shrinks_down_to_one_then_refuses():
    gate = _NvencGate(3)
    assert gate.cap == 3
    assert gate.shrink() == 2
    assert gate.shrink() == 1
    assert gate.shrink() is None
    assert gate.cap == 1


def test_backoff_retries_with_a_shrinking_cap_until_it_succeeds():
    gate = _NvencGate(3)
    attempts = []

    def flaky(tools, clip, plan, enc, dest, threads, token):
        attempts.append(gate.cap)
        if len(attempts) < 3:
            raise RenderError(_NVENC_CAPACITY_MESSAGE)

    _render_with_nvenc_backoff(gate, flaky, None, None, None, None, None, None, None)
    assert attempts == [3, 2, 1]


def test_backoff_gives_up_once_the_cap_is_already_one():
    gate = _NvencGate(1)

    def always_fails(*a, **k):
        raise RenderError(_NVENC_CAPACITY_MESSAGE)

    with pytest.raises(RenderError):
        _render_with_nvenc_backoff(gate, always_fails, None, None, None, None, None, None, None)


def test_backoff_does_not_catch_unrelated_render_errors():
    gate = _NvencGate(2)

    def fails_for_another_reason(*a, **k):
        raise RenderError("クリップ 1: フレーム数が一致しません(期待 10、実際 9)")

    with pytest.raises(RenderError):
        _render_with_nvenc_backoff(gate, fails_for_another_reason, None, None, None, None, None, None, None)


def test_backoff_is_a_no_op_without_a_gate():
    calls = []

    def renderer(*a, **k):
        calls.append(a)

    _render_with_nvenc_backoff(None, renderer, "tools", "clip", "plan", "enc", "dest", "threads", "token")
    assert calls == [("tools", "clip", "plan", "enc", "dest", "threads", "token")]
