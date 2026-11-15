"""Plan: the explicit, serialisable timeline (SPEC §5.4).

The renderer needs nothing but a Plan; it never draws random numbers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import ConfigError

PLAN_VERSION = 1
MATRICES = ("bt601", "bt709", "bt470", "fcc", "smpte240m", "bt2020")


@dataclass
class SourceSpec:
    path: Path
    in_point: float      # seconds into the source file
    span: float          # seconds of source consumed
    loop: bool = False   # source is shorter than needed: repeat from the start
    matrix: str = "bt709"  # YCbCr matrix of the source file (colour conversion to BT.709)


@dataclass
class EffectSpec:
    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClipSpec:
    index: int
    start_frame: int
    frames: int
    source: SourceSpec
    effects: list[EffectSpec] = field(default_factory=list)


@dataclass
class OutputSpec:
    width: int
    height: int
    fps: Fraction
    duration: float
    frames: int


@dataclass
class AudioSpec:
    """Music: the file to mix in (None for a silent video driven by a manual BPM) and its beat grid."""

    path: Path | None
    start: float = 0.0
    bpm: float | None = None
    beat_offset: float | None = None


@dataclass
class Plan:
    seed: int
    cut_mode: str
    output: OutputSpec
    clips: list[ClipSpec]
    audio: AudioSpec | None = None
    version: int = PLAN_VERSION
    generator: str = f"moviekaleidoscope {__version__}"

    # ---- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        o = self.output
        return {
            "version": self.version,
            "generator": self.generator,
            "seed": self.seed,
            "cut_mode": self.cut_mode,
            "output": {
                "width": o.width, "height": o.height,
                "fps": f"{o.fps.numerator}/{o.fps.denominator}",
                "duration": o.duration, "frames": o.frames,
            },
            "audio": None if self.audio is None else {
                "path": None if self.audio.path is None else str(self.audio.path), "start": self.audio.start,
                "bpm": self.audio.bpm, "beat_offset": self.audio.beat_offset,
            },
            "clips": [
                {
                    "index": c.index, "start_frame": c.start_frame, "frames": c.frames,
                    "source": {"path": str(c.source.path), "in": c.source.in_point,
                               "span": c.source.span, "loop": c.source.loop, "matrix": c.source.matrix},
                    "effects": [{"type": e.type, "params": e.params} for e in c.effects],
                }
                for c in self.clips
            ],
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Plan":
        try:
            if d["version"] != PLAN_VERSION:
                raise ConfigError(f"plan のバージョン {d['version']} には対応していません")
            o = d["output"]
            a = d.get("audio")
            plan = cls(
                seed=int(d["seed"]),
                cut_mode=str(d["cut_mode"]),
                output=OutputSpec(int(o["width"]), int(o["height"]), Fraction(o["fps"]),
                                  float(o["duration"]), int(o["frames"])),
                audio=None if a is None else AudioSpec(
                    None if a.get("path") is None else Path(a["path"]), float(a.get("start", 0.0)),
                    None if a.get("bpm") is None else float(a["bpm"]),
                    None if a.get("beat_offset") is None else float(a["beat_offset"])),
                clips=[
                    ClipSpec(
                        index=int(c["index"]), start_frame=int(c["start_frame"]), frames=int(c["frames"]),
                        source=SourceSpec(Path(c["source"]["path"]), float(c["source"]["in"]),
                                          float(c["source"]["span"]), bool(c["source"].get("loop", False)),
                                          str(c["source"].get("matrix", "bt709"))),
                        effects=[EffectSpec(str(e["type"]), dict(e.get("params", {})))
                                 for e in c.get("effects", [])],
                    )
                    for c in d["clips"]
                ],
                version=int(d["version"]),
                generator=str(d.get("generator", "")),
            )
        except ConfigError:
            raise
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            raise ConfigError(f"plan の形式が不正です: {type(e).__name__}: {e}") from None
        plan.validate()
        return plan

    def validate(self) -> None:
        """Clips must tile the timeline exactly."""
        if self.output.width <= 0 or self.output.height <= 0 or self.output.fps <= 0:
            raise ConfigError("plan: 出力の解像度または FPS が不正です")
        a = self.audio
        if a is not None:
            if a.start < 0 or (a.bpm is not None and not a.bpm > 0) or (a.beat_offset is not None and a.beat_offset < 0):
                raise ConfigError("plan: 音声の start / bpm / beat_offset が不正です")
            if self.cut_mode == "beat" and a.bpm is None:
                raise ConfigError("plan: cut_mode が beat なのに bpm がありません")
        pos = 0
        for i, c in enumerate(self.clips):
            if c.index != i or c.start_frame != pos or c.frames < 1:
                raise ConfigError(f"plan: クリップ {i} の位置または長さが不正です")
            if c.source.span <= 0 or c.source.in_point < 0:
                raise ConfigError(f"plan: クリップ {i} の素材指定が不正です")
            if c.source.matrix not in MATRICES:
                raise ConfigError(f"plan: クリップ {i} の色行列が不正です: {c.source.matrix!r}")
            self._validate_effects(c)
            pos += c.frames
        if pos != self.output.frames:
            raise ConfigError(f"plan: クリップの合計フレーム数({pos})が出力のフレーム数({self.output.frames})と一致しません")


    def _validate_effects(self, clip: ClipSpec) -> None:
        from fractions import Fraction as F

        from ..effects import registry
        from ..effects.base import RenderContext

        ctx = RenderContext(float(F(clip.frames) / self.output.fps), clip.frames, self.output.fps,
                            self.output.width, self.output.height,
                            self.audio.bpm if self.audio else None)
        for e in clip.effects:
            effect = registry.get(e.type)
            try:
                if effect.backend_for(e.params) == "frame":
                    effect.frame_processor(e.params, ctx)
                else:
                    effect.pre_filters(e.params, ctx)
                    effect.post_filters(e.params, ctx)
            except (KeyError, TypeError, ValueError) as err:
                raise ConfigError(f"plan: クリップ {clip.index} のエフェクト '{e.type}' のパラメータが不正です"
                                  f"({type(err).__name__}: {err})") from None


def save_plan(plan: Plan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_plan(path: Path) -> Plan:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ConfigError(f"plan を読めません: {path} ({e})") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"plan の JSON が不正です: {path} ({e})") from None
    return Plan.from_dict(data)
