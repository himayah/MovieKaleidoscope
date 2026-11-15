"""Effect registry and the per-run selection pool (SPEC §7, §9)."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING, Any, Mapping

from ..errors import ConfigError
from ..planner.plan import EffectSpec
from .base import Effect, SampleContext
from .feedback import Feedback
from .flip import Flip
from .hue import Hue
from .kaleidoscope import Kaleidoscope
from .mirror import Mirror
from .negate import Negate
from .posterize import Posterize
from .reverse import Reverse
from .rgbshift import RgbShift
from .saturate import Saturate
from .solarize import Solarize
from .speed import Speed
from .strobe import Strobe
from .tile import Tile
from .zoom import Zoom

if TYPE_CHECKING:
    from ..config import EffectsConfig

_EFFECTS: dict[str, Effect] = {
    e.name: e for e in (
        Flip(), Negate(), Mirror(), Solarize(), RgbShift(), Hue(), Saturate(), Posterize(),
        Speed(), Reverse(), Zoom(), Kaleidoscope(), Strobe(), Feedback(), Tile(),
    )
}


def names() -> list[str]:
    return sorted(_EFFECTS)


def get(name: str) -> Effect:
    try:
        return _EFFECTS[name]
    except KeyError:
        raise ConfigError(f"未知のエフェクトです: '{name}'(使えるもの: {', '.join(names())})") from None


def _resolve_overrides(effect: Effect, overrides: Mapping[str, Any]) -> tuple[dict[str, tuple[float, float]], dict[str, float]]:
    """Merge config overrides into (parameter ranges, options) for one effect."""
    ranges = {p.name: (p.lo, p.hi) for p in effect.params}
    opts = dict(effect.options)
    by_name = {p.name: p for p in effect.params}
    for key, value in overrides.items():
        where = f"effects.params.{effect.name}.{key}"
        if key in by_name:
            if not isinstance(value, dict) or set(value) - {"min", "max"}:
                raise ConfigError(f"{where}: {{min: 数値, max: 数値}} の形式で指定してください")
            lo, hi = ranges[key]
            for k, bound in (("min", 0), ("max", 1)):
                if k in value:
                    v = value[k]
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                        raise ConfigError(f"{where}.{k}: 数値を指定してください")
                    if bound == 0:
                        lo = float(v)
                    else:
                        hi = float(v)
            if lo > hi:
                raise ConfigError(f"{where}: min は max 以下にしてください")
            if by_name[key].log and lo <= 0:
                raise ConfigError(f"{where}: min は 0 より大きくしてください")
            ranges[key] = (lo, hi)
        elif key in opts:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ConfigError(f"{where}: 0 より大きい数値を指定してください")
            opts[key] = float(value)
        else:
            known = sorted([*by_name, *opts])
            raise ConfigError(f"{where}: 未知のパラメータです(使えるもの: {', '.join(known) or 'なし'})")
    return ranges, opts


def active_names(cfg: "EffectsConfig") -> list[str]:
    """Effects that may be picked: enabled minus excluded. Validates every name."""
    for n in (*cfg.exclude, *cfg.weights, *cfg.params):
        get(n)  # unknown / not-yet-implemented names are errors
    chosen = names() if cfg.enabled == "all" else list(cfg.enabled)
    for n in chosen:
        get(n)
    out = [n for n in chosen if n not in set(cfg.exclude)]
    if not out:
        raise ConfigError("使えるエフェクトが 1 つもありません(enabled / exclude を確認してください)")
    return out


class EffectPool:
    """Picks an effect (and its parameters) for each clip from the configured set."""

    def __init__(self, cfg: "EffectsConfig") -> None:
        self._names = active_names(cfg)
        self._weights = {n: float(cfg.weights.get(n, 1.0)) for n in self._names}
        self._resolved = {n: _resolve_overrides(_EFFECTS[n], cfg.params.get(n, {})) for n in self._names}
        if not any(w > 0 for w in self._weights.values()):
            raise ConfigError("エフェクトの重みがすべて 0 です")

    def pick(self, rng: random.Random, sctx: SampleContext) -> EffectSpec:
        usable = [n for n in self._names
                  if self._weights[n] > 0 and _EFFECTS[n].available(sctx, self._resolved[n][1])]
        if not usable:
            raise ConfigError(f"この長さ({sctx.duration:.2f} 秒)の断片に使えるエフェクトがありません")
        name = rng.choices(usable, weights=[self._weights[n] for n in usable])[0]
        ranges, opts = self._resolved[name]
        return EffectSpec(name, _EFFECTS[name].sample(rng, sctx, ranges, opts))

    @staticmethod
    def source_span(spec: EffectSpec, duration: float) -> float:
        return get(spec.type).source_span(spec.params, duration)


def validate_effects_config(cfg: "EffectsConfig") -> None:
    EffectPool(cfg)


def describe() -> str:
    """Human-readable list of effects with their parameter ranges (for --list-effects)."""
    lines = []
    for n in names():
        e = _EFFECTS[n]
        backend = "ffmpeg / frame" if n == "rgbshift" else e.backend
        lines.append(f"{n}  [{backend}]  {e.description}")
        for p in e.params:
            scale = "対数" if p.log else "線形"
            lines.append(f"    {p.name}: {p.lo:g}〜{p.hi:g}({scale})  動き: {', '.join(p.motions)}"
                         + (f"  - {p.doc}" if p.doc else ""))
        for k, v in e.options.items():
            lines.append(f"    {k}: 既定 {v:g}")
    lines.append("")
    lines.append("範囲の上書き: 設定ファイルの effects.params.<エフェクト名>.<パラメータ>: {min: 数値, max: 数値}")
    return "\n".join(lines) + "\n"
