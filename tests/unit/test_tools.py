"""locate_tools(): never lets a subprocess failure escape as anything but ToolNotFoundError
(SPEC §11). A raw, unhandled exception here would crash the whole program before rendering even
starts -- on hardware/drivers we haven't seen, that is exactly the failure mode to avoid."""

from __future__ import annotations

import subprocess

import pytest

from moviekaleidoscope.errors import ToolNotFoundError
from moviekaleidoscope.media import tools as tools_mod
from moviekaleidoscope.media.tools import locate_tools, parse_version


@pytest.fixture(autouse=True)
def fake_which(monkeypatch):
    """ffmpeg/ffprobe "exist" at fixed fake paths, without touching the real filesystem/PATH."""
    monkeypatch.setattr(tools_mod.shutil, "which",
                        lambda name: f"/fake/{name}" if name in ("ffmpeg", "ffprobe") else None)


def test_raises_tool_not_found_when_the_version_check_times_out(monkeypatch):
    def raises(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15)
    monkeypatch.setattr(tools_mod, "run_capture", raises)
    with pytest.raises(ToolNotFoundError):
        locate_tools()


def test_raises_tool_not_found_when_the_version_check_cannot_launch(monkeypatch):
    def raises(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr(tools_mod, "run_capture", raises)
    with pytest.raises(ToolNotFoundError):
        locate_tools()


def test_raises_tool_not_found_when_the_version_check_exits_nonzero(monkeypatch):
    monkeypatch.setattr(tools_mod, "run_capture", lambda *a, **k: (1, "", "error"))
    with pytest.raises(ToolNotFoundError):
        locate_tools()


def test_raises_tool_not_found_when_the_version_is_too_old(monkeypatch):
    monkeypatch.setattr(tools_mod, "run_capture",
                        lambda *a, **k: (0, "ffmpeg version 4.3.1-static\n", ""))
    with pytest.raises(ToolNotFoundError):
        locate_tools()


def test_succeeds_and_only_warns_when_the_version_cannot_be_parsed(monkeypatch, caplog):
    monkeypatch.setattr(tools_mod, "run_capture",
                        lambda *a, **k: (0, "ffmpeg version N-12345-g0123456\n", ""))
    result = locate_tools()
    assert result.version is None
    assert result.ffmpeg == "/fake/ffmpeg"
    assert result.ffprobe == "/fake/ffprobe"


def test_succeeds_with_a_recent_version(monkeypatch):
    monkeypatch.setattr(tools_mod, "run_capture",
                        lambda *a, **k: (0, "ffmpeg version 8.1.2-full_build\n", ""))
    result = locate_tools()
    assert result.version == (8, 1)
    assert result.version_text == "ffmpeg version 8.1.2-full_build"


def test_raises_tool_not_found_when_ffmpeg_is_not_on_path(monkeypatch):
    monkeypatch.setattr(tools_mod.shutil, "which", lambda name: None)
    with pytest.raises(ToolNotFoundError):
        locate_tools()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ffmpeg version 4.4.2-0ubuntu0.22.04.1", (4, 4)),
        ("ffmpeg version n6.1", (6, 1)),
        ("ffmpeg version 8.1.2-essentials_build-www.gyan.dev", (8, 1)),
        ("ffmpeg version N-12345-g0123456", None),
        ("not ffmpeg at all", None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected
