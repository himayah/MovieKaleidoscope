from __future__ import annotations

from pathlib import Path

import pytest

from moviekaleidoscope.config import DEFAULT_EXTENSIONS, VideoConfig
from moviekaleidoscope.errors import ConfigError
from moviekaleidoscope.media.inputs import collect_input_paths
from moviekaleidoscope.media.output_format import resolve_output_format
from moviekaleidoscope.media.probe import VideoInfo
from fractions import Fraction


def touch(root: Path, *names: str) -> list[Path]:
    out = []
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        out.append(p)
    return out


def names(paths) -> list[str]:
    return [p.name for p in paths]


def test_folder_natural_order_and_extension_filter(tmp_path):
    touch(tmp_path, "clip10.mp4", "clip2.mp4", "clip1.MOV", "notes.txt", "a.jpg", ".hidden.mp4")
    got = collect_input_paths([tmp_path], False, DEFAULT_EXTENSIONS)
    assert names(got) == ["clip1.MOV", "clip2.mp4", "clip10.mp4"]


def test_recursive_flag(tmp_path):
    touch(tmp_path, "a.mp4", "sub/b.mp4", "sub/deep/c.mp4")
    assert names(collect_input_paths([tmp_path], False, DEFAULT_EXTENSIONS)) == ["a.mp4"]
    assert sorted(names(collect_input_paths([tmp_path], True, DEFAULT_EXTENSIONS))) == ["a.mp4", "b.mp4", "c.mp4"]


def test_given_order_is_kept_and_duplicates_removed(tmp_path):
    z, a = touch(tmp_path / "f", "z.mp4", "a.mp4")
    got = collect_input_paths([z, tmp_path / "f", a, z], False, DEFAULT_EXTENSIONS)
    assert got == [z, a]  # z first (as given), folder adds only a, duplicates dropped


def test_direct_file_ignores_extension_filter(tmp_path):
    (f,) = touch(tmp_path, "weird.xyz")
    assert collect_input_paths([f], False, ["mp4"]) == [f]


def test_missing_and_empty_inputs_are_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="見つかりません"):
        collect_input_paths([tmp_path / "nope.mp4"], False, DEFAULT_EXTENSIONS)
    with pytest.raises(ConfigError):
        collect_input_paths([], False, DEFAULT_EXTENSIONS)


def test_empty_folder_gives_empty_list(tmp_path):
    assert collect_input_paths([tmp_path], False, DEFAULT_EXTENSIONS) == []


# ---- output format ----------------------------------------------------------
def info(w=1280, h=720, fps=Fraction(30)) -> VideoInfo:
    return VideoInfo(Path("x.mp4"), w, h, fps, 10.0)


def test_format_defaults_to_first_input():
    f = resolve_output_format(VideoConfig(), info(1920, 1080, Fraction(30000, 1001)))
    assert (f.width, f.height, f.fps) == (1920, 1080, Fraction(30000, 1001))


def test_format_rounds_to_even():
    f = resolve_output_format(VideoConfig(), info(1281, 721))
    assert (f.width, f.height) == (1280, 720)


@pytest.mark.parametrize("src,expected", [
    ((3840, 2160), (1920, 1080)),
    ((2160, 3840), (1080, 1920)),       # vertical 4K
    ((2560, 1440), (1920, 1080)),
    ((4096, 2160), (1920, 1012)),       # DCI 4K: long side limits (1013 rounded to even)
    ((1920, 1080), (1920, 1080)),
    ((1440, 1440), (1080, 1080)),       # square: short side limits
    ((1280, 720), (1280, 720)),
])
def test_format_default_is_at_most_1080p(src, expected):
    f = resolve_output_format(VideoConfig(), info(*src))
    assert (f.width, f.height) == expected


def test_format_explicit_size_may_exceed_1080p():
    f = resolve_output_format(VideoConfig(width=3840, height=2160), info(3840, 2160))
    assert (f.width, f.height) == (3840, 2160)
    f = resolve_output_format(VideoConfig(width=3840), info(3840, 2160))
    assert (f.width, f.height) == (3840, 2160)


def test_format_one_dimension_keeps_aspect():
    f = resolve_output_format(VideoConfig(width=640), info(1920, 1080))
    assert (f.width, f.height) == (640, 360)
    f = resolve_output_format(VideoConfig(height=480), info(1920, 1080))
    assert (f.width, f.height) == (852, 480)


def test_format_overrides():
    f = resolve_output_format(VideoConfig(width=800, height=600, fps=Fraction(24)), info())
    assert (f.width, f.height, f.fps) == (800, 600, Fraction(24))


@pytest.mark.parametrize("src,expected", [
    (Fraction(3120, 179), Fraction(15)),               # VFR sample (~17.43)
    (Fraction(2997003, 100000), Fraction(30000, 1001)),  # phone-style 29.97003
    (Fraction(5994, 100), Fraction(60000, 1001)),
    (Fraction(2500, 99), Fraction(25)),                 # ~25.25
    (Fraction(27), Fraction(25)),                       # 27/25 = 1.08 is closer than 29.97/27 = 1.11
    (Fraction(1), Fraction(15)),
    (Fraction(119), Fraction(120)),
])
def test_format_default_fps_snaps_to_standard(src, expected):
    assert resolve_output_format(VideoConfig(), info(fps=src)).fps == expected


@pytest.mark.parametrize("fps", [Fraction(24000, 1001), Fraction(25), Fraction(30000, 1001), Fraction(60)])
def test_format_standard_fps_kept(fps):
    assert resolve_output_format(VideoConfig(), info(fps=fps)).fps == fps


def test_format_explicit_fps_not_snapped():
    assert resolve_output_format(VideoConfig(fps=Fraction(17)), info()).fps == Fraction(17)


@pytest.mark.parametrize("fps", [Fraction(1, 2), Fraction(240)])
def test_format_fps_out_of_range_falls_back_to_30(fps):
    assert resolve_output_format(VideoConfig(), info(fps=fps)).fps == Fraction(30)
