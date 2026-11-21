"""GUI settings model: what the form holds, and how it becomes a Config (no tkinter here).

Every field is text, exactly as typed in the form; an empty string means "not set". The
conversion to a Config goes through the same validation as the command line, so the two can
never disagree about what is valid. Settings loaded from a config file keep their non-GUI
parts (effect weights, parameter ranges, extensions, ...) in `base` and write them back.
"""

from __future__ import annotations

import copy
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import (
    CUT_MODES, X264_PRESETS, Config, build_config, config_from_dict, merge_config_dicts, validate_for_run,
)
from ..effects import registry
from ..errors import ConfigError

MODES = CUT_MODES


@dataclass
class GuiSettings:
    inputs: list[str] = field(default_factory=list)
    recursive: bool = False
    output: str = ""
    overwrite: bool = False
    duration: str = ""                 # "", seconds, or mm:ss
    audio_file: str = ""
    audio_start: str = ""
    bpm: str = ""                      # "" = detect
    cut_mode: str = "auto"
    cut_fixed: str = "2"
    cut_random_min: str = "1"
    cut_random_max: str = "4"
    beats: str = "4"                   # "4" or "2,4"
    width: str = ""
    height: str = ""
    fps: str = ""
    crf: str = "18"
    preset: str = "medium"
    seed: str = ""
    jobs: str = ""                     # "" = automatic
    ffmpeg: str = ""
    effects: dict[str, bool] = field(default_factory=lambda: {n: True for n in registry.names()})
    # Parts of a loaded config file that the form has no field for; written back unchanged.
    base: dict[str, Any] = field(default_factory=dict, repr=False)


def _num(text: str, label: str, kind=float, **limits) -> Any:
    try:
        v = kind(text.strip())
    except ValueError:
        raise ConfigError(f"{label}: 数値で入力してください(入力: {text!r})") from None
    if not math.isfinite(v):
        raise ConfigError(f"{label}: 有限の数値で入力してください")
    return v


def _opt(text: str) -> str | None:
    t = text.strip()
    return t or None


def to_overrides(s: GuiSettings) -> dict[str, Any]:
    """Every field the form manages, as a config-file-shaped dict. Blank fields become None so
    that nothing from `base` leaks through where the user cleared a value."""
    if s.cut_mode not in MODES:
        raise ConfigError(f"カット方式が不正です: {s.cut_mode!r}")
    if s.preset not in X264_PRESETS:
        raise ConfigError(f"プリセットが不正です: {s.preset!r}")
    beats = [x for x in s.beats.replace(",", " ").replace("、", " ").split() if x]
    if not beats:
        raise ConfigError("拍数: 1 つ以上の整数を入力してください(例: 4 または 2,4)")
    o: dict[str, Any] = {
        "inputs": [p for p in (x.strip() for x in s.inputs) if p],
        "recursive": s.recursive,
        "output": _opt(s.output),
        "overwrite": s.overwrite,
        "duration": _opt(s.duration),
        "seed": None if not s.seed.strip() else _num(s.seed, "seed", int),
        "video": {
            "width": None if not s.width.strip() else _num(s.width, "幅", int),
            "height": None if not s.height.strip() else _num(s.height, "高さ", int),
            "fps": _opt(s.fps),
            "crf": _num(s.crf or "18", "CRF", int),
            "preset": s.preset,
        },
        "audio": {
            "file": _opt(s.audio_file),
            "start": _num(s.audio_start, "音声の開始位置", float) if s.audio_start.strip() else 0.0,
            "bpm": None if not s.bpm.strip() else _num(s.bpm, "BPM", float),
        },
        "cut": {
            "mode": s.cut_mode,
            "fixed": _num(s.cut_fixed or "2", "固定の長さ", float),
            "random": {"min": _num(s.cut_random_min or "1", "ランダムの最小", float),
                       "max": _num(s.cut_random_max or "4", "ランダムの最大", float)},
            "beats": [_num(b, "拍数", int) for b in beats],
        },
        "effects": {"enabled": "all", "exclude": [n for n, on in s.effects.items() if not on]},
        "runtime": {
            "jobs": "auto" if not s.jobs.strip() else _num(s.jobs, "並列数", int),
            "ffmpeg": _opt(s.ffmpeg),
        },
    }
    return o


def to_config(s: GuiSettings, *, check_runnable: bool = True) -> Config:
    """Validate and build a Config. Raises ConfigError with a message fit for a dialog."""
    merged = merge_config_dicts(copy.deepcopy(s.base), to_overrides(s))
    cfg = config_from_dict(merged)
    registry.validate_effects_config(cfg.effects)       # e.g. every effect unticked
    if check_runnable:
        validate_for_run(cfg)
    return cfg


def from_config(cfg: Config) -> GuiSettings:
    """Fill the form from a Config; what the form cannot show goes to `base`."""
    full = cfg.to_file_dict()
    enabled = registry.names() if cfg.effects.enabled == "all" else list(cfg.effects.enabled)
    s = GuiSettings(
        inputs=[str(p) for p in cfg.inputs], recursive=cfg.recursive,
        output="" if cfg.output is None else str(cfg.output), overwrite=cfg.overwrite,
        duration="" if cfg.duration is None else format_seconds(cfg.duration),
        audio_file="" if cfg.audio.file is None else str(cfg.audio.file),
        audio_start="" if cfg.audio.start == 0 else format_seconds(cfg.audio.start),
        bpm="" if cfg.audio.bpm is None else f"{cfg.audio.bpm:g}",
        cut_mode=cfg.cut.mode, cut_fixed=f"{cfg.cut.fixed:g}",
        cut_random_min=f"{cfg.cut.random_min:g}", cut_random_max=f"{cfg.cut.random_max:g}",
        beats=",".join(str(b) for b in cfg.cut.beats),
        width="" if cfg.video.width is None else str(cfg.video.width),
        height="" if cfg.video.height is None else str(cfg.video.height),
        fps="" if cfg.video.fps is None else str(cfg.video.fps),
        crf=str(cfg.video.crf), preset=cfg.video.preset,
        seed="" if cfg.seed is None else str(cfg.seed),
        jobs="" if cfg.runtime.jobs is None else str(cfg.runtime.jobs),
        ffmpeg=cfg.runtime.ffmpeg or "",
        effects={n: (n in enabled and n not in cfg.effects.exclude) for n in registry.names()},
    )
    # Keep what has no field: weights, ranges, extensions, plan path, audio window and offset, ...
    s.base = {
        "extensions": full["extensions"], "plan": full["plan"],
        "video": {"backend": full["video"]["backend"]},
        "audio": {"beat_offset": full["audio"]["beat_offset"], "bpm_range": full["audio"]["bpm_range"]},
        "effects": {"weights": full["effects"]["weights"], "params": full["effects"]["params"]},
        "runtime": {k: full["runtime"][k] for k in ("work_dir", "keep_temp", "ffprobe", "power_plan")},
    }
    return s


def format_seconds(x: float) -> str:
    return f"{x:g}"


def load_settings_file(path: Path) -> GuiSettings:
    """Read a config file (the same YAML the command line uses)."""
    return from_config(build_config(path, {}, Path.cwd()))


def save_settings_file(s: GuiSettings, path: Path) -> None:
    """Write the form as a config file the command line can read too. Relative paths in the form
    are written as typed; they are interpreted relative to the file, as for any config file."""
    cfg = to_config(s, check_runnable=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.to_file_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8")


def settings_dir() -> Path:
    """Where the GUI remembers its last settings (per user)."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "MovieKaleidoscope"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "moviekaleidoscope"


def remember(s: GuiSettings, directory: Path | None = None) -> None:
    try:
        save_settings_file(s, (directory or settings_dir()) / "last.yaml")
    except (ConfigError, OSError):
        pass              # remembering is a convenience: never worth an error


def recall(directory: Path | None = None) -> GuiSettings:
    path = (directory or settings_dir()) / "last.yaml"
    try:
        s = load_settings_file(path)
    except (ConfigError, OSError):
        return GuiSettings()
    s.overwrite = False   # never silently overwrite on the next launch
    return s

