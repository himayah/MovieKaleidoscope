"""resolve_encoder_backend(), encoder_args(backend="nvenc") and the NVENC concurrent-session
backoff (GPU redesign SPEC §A)."""

from __future__ import annotations

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
