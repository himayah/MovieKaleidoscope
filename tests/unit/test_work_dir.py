"""_make_work_dir() / _pick_fastest_free_drive() (GPU redesign SPEC §D)."""

from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path

import pytest

from moviekaleidoscope.config import RuntimeConfig
from moviekaleidoscope.render import pipeline

Usage = namedtuple("usage", "total used free")

# Real Windows only: pathlib refuses to instantiate WindowsPath on another OS (its
# "is_supported" check is fixed at interpreter start), so monkeypatching os.name alone
# cannot fake these on Linux/macOS -- unlike pipeline._choose_work_drive()'s own
# `os.name != "nt"` early return, which these tests exercise from the Windows side.
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="drive-letter paths are a Windows thing")


@pytest.fixture(autouse=True)
def clear_cache():
    pipeline._work_drive_cache = pipeline._UNSET
    yield
    pipeline._work_drive_cache = pipeline._UNSET


def test_pick_fastest_free_drive_picks_most_free(monkeypatch):
    free = {"C:\\": 10 * 2**30, "D:\\": 500 * 2**30}
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: Usage(1, 0, free[str(p)]))
    assert pipeline._pick_fastest_free_drive([Path("C:\\"), Path("D:\\")]) == Path("D:\\")


def test_pick_fastest_free_drive_skips_unreadable_candidates(monkeypatch):
    def fake(p):
        if str(p).startswith("Z"):
            raise OSError("not ready")
        return Usage(1, 0, 5 * 2**30)

    monkeypatch.setattr(pipeline.shutil, "disk_usage", fake)
    assert pipeline._pick_fastest_free_drive([Path("Z:\\"), Path("C:\\")]) == Path("C:\\")


def test_pick_fastest_free_drive_none_when_nothing_readable(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError()))
    assert pipeline._pick_fastest_free_drive([Path("Z:\\")]) is None


def test_explicit_work_dir_is_used_unchanged(tmp_path):
    rc = RuntimeConfig(work_dir=tmp_path / "w")
    out = pipeline._make_work_dir(rc)
    assert out.parent == tmp_path / "w"


@windows_only
def test_keeps_the_temp_drive_when_it_has_enough_space(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: Usage(1, 0, pipeline.MIN_FREE_BYTES + 1))
    assert pipeline._choose_work_drive() == Path(Path(tmp_path).anchor)


@windows_only
def test_switches_drive_when_the_temp_drive_is_low_on_space(monkeypatch, tmp_path):
    temp_anchor = Path(tmp_path).anchor
    monkeypatch.setattr(pipeline.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake(p):
        return Usage(1, 0, 1) if str(p) == temp_anchor else Usage(1, 0, pipeline.MIN_FREE_BYTES * 2)

    monkeypatch.setattr(pipeline.shutil, "disk_usage", fake)
    monkeypatch.setattr(pipeline, "_fixed_drive_roots", lambda: [Path("D:\\")])
    assert pipeline._choose_work_drive() == Path("D:\\")


@windows_only
def test_choose_work_drive_is_cached(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pipeline.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake(p):
        calls.append(p)
        return Usage(1, 0, pipeline.MIN_FREE_BYTES + 1)

    monkeypatch.setattr(pipeline.shutil, "disk_usage", fake)
    pipeline._choose_work_drive()
    pipeline._choose_work_drive()
    assert len(calls) == 1


def test_choose_work_drive_is_none_outside_windows(monkeypatch):
    monkeypatch.setattr(pipeline.os, "name", "posix")
    assert pipeline._choose_work_drive() is None
