"""Configuration: schema, YAML loading, CLI override merge, validation (SPEC §9).

Precedence: CLI > config file > defaults. Unknown keys are errors.
Relative paths in the config file resolve against the config file's directory;
relative paths given on the CLI resolve against the current directory.
"""

from __future__ import annotations

import copy
import math
import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)
VIDEO_BACKENDS = ("auto", "software", "nvenc")
CUT_MODES = ("auto", "fixed", "random", "beat")
DEFAULT_EXTENSIONS = (
    "mp4", "mov", "mkv", "avi", "webm", "m4v", "mpg", "mpeg", "wmv", "flv",
)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
@dataclass
class VideoConfig:
    width: int | None = None
    height: int | None = None
    fps: Fraction | None = None
    crf: int = 18
    preset: str = "medium"
    backend: str = "auto"   # "auto" | "software" | "nvenc"


@dataclass
class AudioConfig:
    file: Path | None = None
    start: float = 0.0
    bpm: float | None = None
    beat_offset: float | None = None
    bpm_range: tuple[float, float] = (70.0, 180.0)


@dataclass
class CutConfig:
    mode: str = "auto"
    fixed: float = 2.0
    random_min: float = 1.0
    random_max: float = 4.0
    beats: tuple[int, ...] = (4,)


@dataclass
class EffectsConfig:
    enabled: str | tuple[str, ...] = "all"
    exclude: tuple[str, ...] = ()
    weights: dict[str, float] = field(default_factory=dict)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RuntimeConfig:
    jobs: int | None = None  # None = auto
    work_dir: Path | None = None
    keep_temp: bool = False
    ffmpeg: str | None = None
    ffprobe: str | None = None
    power_plan: bool = True  # temporarily switch to "High performance" while rendering (Windows)


@dataclass
class Config:
    inputs: tuple[Path, ...] = ()
    recursive: bool = False
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    output: Path | None = None
    plan: Path | None = None
    duration: float | None = None
    seed: int | None = None
    overwrite: bool = False
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    cut: CutConfig = field(default_factory=CutConfig)
    effects: EffectsConfig = field(default_factory=EffectsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    def to_file_dict(self) -> dict[str, Any]:
        """The same shape as a config file: `config_from_dict(cfg.to_file_dict()) == cfg`.

        Used to save settings (GUI) and for --dump-config, whose output can be loaded back."""
        c, v, a, e, r = self.cut, self.video, self.audio, self.effects, self.runtime
        return {
            "inputs": [str(p) for p in self.inputs],
            "recursive": self.recursive,
            "extensions": list(self.extensions),
            "output": None if self.output is None else str(self.output),
            "plan": None if self.plan is None else str(self.plan),
            "duration": self.duration,
            "seed": self.seed,
            "overwrite": self.overwrite,
            "video": {"width": v.width, "height": v.height, "fps": None if v.fps is None else str(v.fps),
                      "crf": v.crf, "preset": v.preset, "backend": v.backend},
            "audio": {"file": None if a.file is None else str(a.file), "start": a.start, "bpm": a.bpm,
                      "beat_offset": a.beat_offset, "bpm_range": list(a.bpm_range)},
            "cut": {"mode": c.mode, "fixed": c.fixed, "random": {"min": c.random_min, "max": c.random_max},
                    "beats": list(c.beats)},
            "effects": {"enabled": e.enabled if isinstance(e.enabled, str) else list(e.enabled),
                        "exclude": list(e.exclude), "weights": dict(e.weights),
                        "params": {k: dict(x) for k, x in e.params.items()}},
            "runtime": {"jobs": "auto" if r.jobs is None else r.jobs,
                        "work_dir": None if r.work_dir is None else str(r.work_dir),
                        "keep_temp": r.keep_temp, "ffmpeg": r.ffmpeg, "ffprobe": r.ffprobe,
                        "power_plan": r.power_plan},
        }


# --------------------------------------------------------------------------
# Value parsers
# --------------------------------------------------------------------------
def _number(value: Any, where: str, *, integer: bool = False,
            gt: float | None = None, ge: float | None = None,
            le: float | None = None) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: 数値を指定してください(値: {value!r})")
    if integer and isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"{where}: 整数を指定してください(値: {value!r})")
    v = int(value) if integer else float(value)
    if not math.isfinite(v):
        raise ConfigError(f"{where}: 有限の数値を指定してください")
    if gt is not None and not v > gt:
        raise ConfigError(f"{where}: {gt} より大きい値を指定してください(値: {v})")
    if ge is not None and not v >= ge:
        raise ConfigError(f"{where}: {ge} 以上の値を指定してください(値: {v})")
    if le is not None and not v <= le:
        raise ConfigError(f"{where}: {le} 以下の値を指定してください(値: {v})")
    return v


def _opt_number(value: Any, where: str, **kw: Any) -> Any:
    return None if value is None else _number(value, where, **kw)


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: true / false を指定してください(値: {value!r})")
    return value


def parse_duration(value: Any, where: str = "duration") -> float:
    """Seconds, or a 'mm:ss' / 'hh:mm:ss' string."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: 長さの形式が不正です(値: {value!r})")
    if isinstance(value, (int, float)):
        d = float(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            if ":" in s:
                parts = s.split(":")
                if len(parts) > 3:
                    raise ValueError
                nums = [float(p) for p in parts]
                if any(n < 0 for n in nums):
                    raise ValueError
                d = 0.0
                for n in nums:
                    d = d * 60 + n
            else:
                d = float(s)
        except ValueError:
            raise ConfigError(f"{where}: 長さの形式が不正です(値: {value!r})") from None
    else:
        raise ConfigError(f"{where}: 長さの形式が不正です(値: {value!r})")
    if not math.isfinite(d) or d <= 0:
        raise ConfigError(f"{where}: 0 より大きい長さを指定してください(値: {value!r})")
    return d


def parse_fps(value: Any, where: str = "video.fps") -> Fraction:
    """Number or 'a/b' string -> exact Fraction. 29.97 etc. snap to NTSC rationals."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: FPS の形式が不正です(値: {value!r})")
    explicit_ratio = isinstance(value, str) and "/" in value
    try:
        if isinstance(value, (int, float)):
            f = Fraction(value).limit_denominator(1000)
        elif isinstance(value, str):
            f = Fraction(value.strip())
        else:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise ConfigError(f"{where}: FPS の形式が不正です(値: {value!r})") from None
    if not (0 < f <= 240):
        raise ConfigError(f"{where}: 0 より大きく 240 以下の FPS を指定してください(値: {value!r})")
    if not explicit_ratio and f.denominator != 1:
        for base in (24, 30, 60, 120):
            ntsc = Fraction(base * 1000, 1001)
            if abs(float(f) - float(ntsc)) < 0.002:
                return ntsc
    return f


def _section(raw: dict, name: str) -> dict:
    v = raw.get(name)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(f"{name}: マッピング(キー: 値)で指定してください")
    return v


def _check_keys(d: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = [k for k in d if k not in allowed]
    if unknown:
        prefix = f"{where}." if where else ""
        names = ", ".join(f"{prefix}{k}" for k in unknown)
        raise ConfigError(f"未知のキーがあります: {names}")


def _path_list(value: Any, where: str) -> tuple[Path, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, os.PathLike)):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, (str, os.PathLike)) for x in value):
        raise ConfigError(f"{where}: パスのリストで指定してください")
    return tuple(Path(x) for x in value)


def _opt_path(value: Any, where: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError(f"{where}: パスを指定してください(値: {value!r})")
    return Path(value)


def _opt_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}: 文字列を指定してください(値: {value!r})")
    return value


def _name_list(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{where}: 名前のリストで指定してください")
    return tuple(value)


# --------------------------------------------------------------------------
# dict -> Config
# --------------------------------------------------------------------------
_TOP_KEYS = ("inputs", "recursive", "extensions", "output", "plan", "duration",
             "seed", "overwrite", "video", "audio", "cut", "effects", "runtime")


def config_from_dict(raw: dict) -> Config:
    """Validate a merged raw dict (paths already resolved) into a Config."""
    _check_keys(raw, _TOP_KEYS, "")
    cfg = Config()

    cfg.inputs = _path_list(raw.get("inputs"), "inputs")
    if "recursive" in raw:
        cfg.recursive = _boolean(raw["recursive"], "recursive")
    if "overwrite" in raw:
        cfg.overwrite = _boolean(raw["overwrite"], "overwrite")
    if raw.get("extensions") is not None:
        exts = _name_list(raw["extensions"], "extensions")
        cfg.extensions = tuple(e.lower().lstrip(".") for e in exts)
    cfg.output = _opt_path(raw.get("output"), "output")
    cfg.plan = _opt_path(raw.get("plan"), "plan")
    if raw.get("duration") is not None:
        cfg.duration = parse_duration(raw["duration"])
    cfg.seed = _opt_number(raw.get("seed"), "seed", integer=True, ge=0)

    # video
    v = _section(raw, "video")
    _check_keys(v, ("width", "height", "fps", "crf", "preset", "backend"), "video")
    cfg.video.width = _opt_number(v.get("width"), "video.width", integer=True, gt=0)
    cfg.video.height = _opt_number(v.get("height"), "video.height", integer=True, gt=0)
    if v.get("fps") is not None:
        cfg.video.fps = parse_fps(v["fps"])
    if "crf" in v:
        cfg.video.crf = _number(v["crf"], "video.crf", integer=True, ge=0, le=51)
    if "preset" in v:
        if v["preset"] not in X264_PRESETS:
            raise ConfigError(f"video.preset: {', '.join(X264_PRESETS)} のいずれかを指定してください")
        cfg.video.preset = v["preset"]
    if "backend" in v:
        if v["backend"] not in VIDEO_BACKENDS:
            raise ConfigError(f"video.backend: {', '.join(VIDEO_BACKENDS)} のいずれかを指定してください")
        cfg.video.backend = v["backend"]

    # audio
    a = _section(raw, "audio")
    _check_keys(a, ("file", "start", "bpm", "beat_offset", "bpm_range"), "audio")
    cfg.audio.file = _opt_path(a.get("file"), "audio.file")
    if "start" in a:
        cfg.audio.start = _number(a["start"], "audio.start", ge=0)
    cfg.audio.bpm = _opt_number(a.get("bpm"), "audio.bpm", gt=0)
    cfg.audio.beat_offset = _opt_number(a.get("beat_offset"), "audio.beat_offset", ge=0)
    if "bpm_range" in a:
        r = a["bpm_range"]
        if not isinstance(r, (list, tuple)) or len(r) != 2:
            raise ConfigError("audio.bpm_range: [最小, 最大] の形式で指定してください")
        lo = _number(r[0], "audio.bpm_range[0]", gt=0)
        hi = _number(r[1], "audio.bpm_range[1]", gt=0)
        if not lo < hi:
            raise ConfigError("audio.bpm_range: 最小は最大より小さくしてください")
        cfg.audio.bpm_range = (lo, hi)

    # cut
    c = _section(raw, "cut")
    _check_keys(c, ("mode", "fixed", "random", "beats"), "cut")
    if "mode" in c:
        if c["mode"] not in CUT_MODES:
            raise ConfigError(f"cut.mode: {', '.join(CUT_MODES)} のいずれかを指定してください")
        cfg.cut.mode = c["mode"]
    if "fixed" in c:
        cfg.cut.fixed = _number(c["fixed"], "cut.fixed", gt=0)
    if "random" in c:
        r = c["random"]
        if not isinstance(r, dict):
            raise ConfigError("cut.random: {min: 秒, max: 秒} の形式で指定してください")
        _check_keys(r, ("min", "max"), "cut.random")
        lo = _number(r.get("min", cfg.cut.random_min), "cut.random.min", gt=0)
        hi = _number(r.get("max", cfg.cut.random_max), "cut.random.max", gt=0)
        if lo > hi:
            raise ConfigError("cut.random: min は max 以下にしてください")
        cfg.cut.random_min, cfg.cut.random_max = lo, hi
    if "beats" in c:
        b = c["beats"]
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            b = [b]
        if not isinstance(b, (list, tuple)) or not b:
            raise ConfigError("cut.beats: 1 以上の整数のリストで指定してください")
        cfg.cut.beats = tuple(_number(x, "cut.beats", integer=True, ge=1) for x in b)

    # effects (names are validated against the registry once it exists)
    e = _section(raw, "effects")
    _check_keys(e, ("enabled", "exclude", "weights", "params"), "effects")
    if e.get("enabled") is not None and e["enabled"] != "all":
        cfg.effects.enabled = _name_list(e["enabled"], "effects.enabled")
    if e.get("exclude"):
        cfg.effects.exclude = _name_list(e["exclude"], "effects.exclude")
    w = e.get("weights") or {}
    if not isinstance(w, dict):
        raise ConfigError("effects.weights: {エフェクト名: 重み} の形式で指定してください")
    cfg.effects.weights = {str(k): _number(x, f"effects.weights.{k}", ge=0) for k, x in w.items()}
    p = e.get("params") or {}
    if not isinstance(p, dict) or not all(isinstance(x, dict) for x in p.values()):
        raise ConfigError("effects.params: {エフェクト名: {パラメータ: 値}} の形式で指定してください")
    cfg.effects.params = {str(k): dict(x) for k, x in p.items()}

    # runtime
    r = _section(raw, "runtime")
    _check_keys(r, ("jobs", "work_dir", "keep_temp", "ffmpeg", "ffprobe", "power_plan"), "runtime")
    jobs = r.get("jobs", "auto")
    cfg.runtime.jobs = None if jobs in (None, "auto") else _number(jobs, "runtime.jobs", integer=True, ge=1)
    cfg.runtime.work_dir = _opt_path(r.get("work_dir"), "runtime.work_dir")
    if "keep_temp" in r:
        cfg.runtime.keep_temp = _boolean(r["keep_temp"], "runtime.keep_temp")
    cfg.runtime.ffmpeg = _opt_str(r.get("ffmpeg"), "runtime.ffmpeg")
    cfg.runtime.ffprobe = _opt_str(r.get("ffprobe"), "runtime.ffprobe")
    if "power_plan" in r:
        cfg.runtime.power_plan = _boolean(r["power_plan"], "runtime.power_plan")
    return cfg


# --------------------------------------------------------------------------
# Loading, path resolution, merging
# --------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigError(f"設定ファイルを読めません: {path} ({e})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"設定ファイルの YAML が不正です: {path}\n{e}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"設定ファイルの最上位はマッピングにしてください: {path}")
    return data


def _abs(value: Any, base: Path) -> Any:
    if isinstance(value, (str, os.PathLike)):
        p = Path(value).expanduser()
        return p if p.is_absolute() else base / p
    return value  # invalid types are reported by validation


def _looks_like_path(s: str) -> bool:
    return "/" in s or "\\" in s or s.startswith(".")


def _resolve_paths(raw: dict, base: Path) -> dict:
    out = copy.deepcopy(raw)
    for key in ("output", "plan"):
        if key in out:
            out[key] = _abs(out[key], base)
    if "inputs" in out:
        v = out["inputs"]
        if isinstance(v, (str, os.PathLike)):
            v = [v]
        if isinstance(v, (list, tuple)):
            out["inputs"] = [_abs(x, base) for x in v]
    for section, keys in (("audio", ("file",)), ("runtime", ("work_dir",))):
        d = out.get(section)
        if isinstance(d, dict):
            for k in keys:
                if k in d:
                    d[k] = _abs(d[k], base)
    rt = out.get("runtime")
    if isinstance(rt, dict):
        for k in ("ffmpeg", "ffprobe"):
            if isinstance(rt.get(k), str) and _looks_like_path(rt[k]):
                rt[k] = str(_abs(rt[k], base))
    return out


def merge_config_dicts(base: dict, over: dict) -> dict:
    """Deep merge: nested mappings merge, everything else (lists, None) is replaced."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_config_dicts(out[k], v)
        else:
            out[k] = v
    return out


def build_config(config_file: str | os.PathLike | None,
                 overrides: dict | None,
                 cwd: str | os.PathLike) -> Config:
    """Merge defaults < config file < CLI overrides and validate."""
    cwd = Path(cwd)
    raw_file: dict = {}
    if config_file is not None:
        cf = _abs(config_file, cwd)
        raw_file = _resolve_paths(_load_yaml(cf), cf.parent)
    raw_cli = _resolve_paths(overrides or {}, cwd)
    return config_from_dict(merge_config_dicts(raw_file, raw_cli))


def _check_output_path(cfg: Config) -> None:
    if cfg.output is None:
        raise ConfigError("出力先が指定されていません(-o または設定ファイルの output)")
    if cfg.output.is_dir():
        raise ConfigError(f"出力先がフォルダです(ファイル名を指定してください): {cfg.output}")
    if cfg.output.suffix.lower() != ".mp4":
        raise ConfigError(f"出力ファイルの拡張子は .mp4 のみ対応しています: {cfg.output}")


def validate_for_run(cfg: Config, *, plan_only: bool = False) -> None:
    """Checks needed before generating. With plan_only, only the plan file is written."""
    if not cfg.inputs:
        raise ConfigError("入力が指定されていません(-i または設定ファイルの inputs)")
    if plan_only and cfg.output is None and cfg.plan is None:
        raise ConfigError("plan の保存先が指定されていません(--plan または -o)")
    if not (plan_only and cfg.output is None):
        _check_output_path(cfg)
    if cfg.duration is None and cfg.audio.file is None:
        raise ConfigError("出力の長さが指定されていません(--duration、または --audio で音声の長さを使用)")
    if not cfg.overwrite:
        plan = cfg.plan or cfg.output.with_name(cfg.output.name + ".plan.json")
        targets = [plan] if plan_only else [cfg.output, plan]
        for p in targets:
            if p.exists():
                raise ConfigError(f"既に存在します(上書きするには -y): {p}")


def validate_for_render(cfg: Config) -> None:
    """Checks for rendering an existing plan (--from-plan): only the output matters."""
    _check_output_path(cfg)
    if not cfg.overwrite and cfg.output.exists():
        raise ConfigError(f"既に存在します(上書きするには -y): {cfg.output}")
