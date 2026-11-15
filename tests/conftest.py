from __future__ import annotations

import json
import subprocess

import pytest

from moviekaleidoscope.config import config_from_dict
from moviekaleidoscope.errors import ToolNotFoundError
from moviekaleidoscope.media.tools import locate_tools


@pytest.fixture(scope="session")
def tools():
    try:
        return locate_tools()
    except ToolNotFoundError as e:
        pytest.skip(f"ffmpeg not available: {e}")


@pytest.fixture(scope="session")
def dummy(tools, tmp_path_factory):
    from tools.make_dummy import build_all

    return build_all(tmp_path_factory.mktemp("dummy"), tools)


@pytest.fixture
def ffprobe_json(tools):
    """ffprobe_json(path, count_frames=False) -> dict(streams, format)."""

    def run(path, count_frames: bool = False) -> dict:
        cmd = [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format"]
        if count_frames:
            cmd.append("-count_frames")
        cp = subprocess.run(cmd + ["-i", str(path)], capture_output=True, check=True)
        return json.loads(cp.stdout)

    return run


@pytest.fixture
def frame_times(tools):
    """frame_times(path) -> list of per-frame timestamps in seconds."""

    def run(path) -> list[float]:
        cp = subprocess.run(
            [tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "frame=best_effort_timestamp_time", "-of", "json", "-i", str(path)],
            capture_output=True, check=True)
        return [float(f["best_effort_timestamp_time"]) for f in json.loads(cp.stdout)["frames"]]

    return run


@pytest.fixture
def make_cfg(tmp_path):
    """make_cfg(inputs, **overrides) -> Config writing into tmp_path (fast encode settings)."""

    def build(inputs, **over):
        raw = {
            "inputs": [str(p) for p in inputs],
            "output": str(tmp_path / "out" / "result.mp4"),
            "duration": 6,
            "seed": 7,
            "cut": {"mode": "fixed", "fixed": 1.5},
            "effects": {"enabled": ["hue"]},       # tests not about effects: one harmless effect
            "video": {"crf": 30, "preset": "ultrafast"},
            "runtime": {"jobs": 2, "work_dir": str(tmp_path / "work")},
        }
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(raw.get(k), dict):
                raw[k] = {**raw[k], **v}
            else:
                raw[k] = v
        return config_from_dict(raw)

    return build
