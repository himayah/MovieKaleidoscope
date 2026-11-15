"""Parameter motion: how a continuous effect parameter changes inside a clip (SPEC §6).

A Motion is plain data (stored in the plan). It can be evaluated numerically
(`value`) or turned into an ffmpeg filter expression in the variable `t`
(`expr`, t = seconds since the start of the clip).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

KINDS = ("const", "ramp", "sine", "pulse")
UNITS = ("sec", "beat")


def num(x: float) -> str:
    """Number for an ffmpeg expression; negatives are parenthesised."""
    s = format(float(x), ".6g")
    return f"({s})" if s.startswith("-") else s


@dataclass(frozen=True)
class Motion:
    kind: str
    base: float                    # const: value / ramp: start / sine: centre / pulse: resting value
    end: float | None = None       # ramp: final value (linear in time over the clip)
    amp: float = 0.0               # sine: amplitude / pulse: height above `base` at the beat
    period: float | None = None    # sine / pulse
    unit: str = "sec"              # unit of `period`: seconds or beats
    phase: float = 0.0             # sine, radians
    decay: float = 3.0             # pulse: exponential decay per beat

    # ---- construction ------------------------------------------------------
    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"motion の種類が不正です: {self.kind!r}")
        if self.unit not in UNITS:
            raise ValueError(f"motion の単位が不正です: {self.unit!r}")
        for name in ("base", "amp", "phase", "decay"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"motion の {name} が有限の数値ではありません")
        if self.kind == "ramp" and (self.end is None or not math.isfinite(self.end)):
            raise ValueError("ramp には end が必要です")
        if self.kind in ("sine", "pulse") and not (self.period and math.isfinite(self.period) and self.period > 0):
            raise ValueError(f"{self.kind} には正の period が必要です")

    @classmethod
    def const(cls, v: float) -> "Motion":
        return cls("const", float(v))

    @classmethod
    def from_dict(cls, d: Any) -> "Motion":
        if not isinstance(d, dict) or "motion" not in d:
            raise ValueError(f"motion の形式が不正です: {d!r}")
        allowed = {"motion", "base", "end", "amp", "period", "unit", "phase", "decay"}
        extra = set(d) - allowed
        if extra:
            raise ValueError(f"motion に未知のキーがあります: {sorted(extra)}")
        try:
            return cls(
                kind=d["motion"], base=float(d["base"]),
                end=None if d.get("end") is None else float(d["end"]),
                amp=float(d.get("amp", 0.0)),
                period=None if d.get("period") is None else float(d["period"]),
                unit=d.get("unit", "sec"), phase=float(d.get("phase", 0.0)),
                decay=float(d.get("decay", 3.0)),
            )
        except (KeyError, TypeError) as e:
            raise ValueError(f"motion の形式が不正です: {d!r} ({e})") from None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"motion": self.kind, "base": self.base}
        if self.kind == "ramp":
            d["end"] = self.end
        elif self.kind == "sine":
            d.update(amp=self.amp, period=self.period, unit=self.unit, phase=self.phase)
        elif self.kind == "pulse":
            d.update(amp=self.amp, period=self.period, unit=self.unit, decay=self.decay)
        return d

    # ---- evaluation ----------------------------------------------------------
    def period_sec(self, bpm: float | None) -> float:
        assert self.period is not None
        if self.unit == "sec":
            return self.period
        if not bpm:
            raise ValueError("拍単位の motion には BPM が必要です")
        return self.period * 60.0 / bpm

    def value(self, t: float, duration: float, bpm: float | None = None) -> float:
        if self.kind == "const":
            return self.base
        if self.kind == "ramp":
            return self.base + (self.end - self.base) * min(1.0, max(0.0, t / duration))
        p = self.period_sec(bpm)
        if self.kind == "sine":
            return self.base + self.amp * math.sin(2 * math.pi * t / p + self.phase)
        return self.base + self.amp * math.exp(-self.decay * (t % p) / p)

    def expr(self, duration: float, bpm: float | None = None) -> str:
        """ffmpeg expression for this motion as a function of `t`."""
        if self.kind == "const":
            return num(self.base)
        if self.kind == "ramp":
            return f"{num(self.base)}+{num(self.end - self.base)}*min(1,t/{num(duration)})"
        p = num(self.period_sec(bpm))
        if self.kind == "sine":
            return f"{num(self.base)}+{num(self.amp)}*sin(2*PI*t/{p}+{num(self.phase)})"
        return f"{num(self.base)}+{num(self.amp)}*exp(-{num(self.decay)}*mod(t,{p})/{p})"

    def mean(self) -> float:
        """Average value over the clip, for the kinds where it is exact (const, ramp)."""
        if self.kind == "const":
            return self.base
        if self.kind == "ramp":
            return (self.base + self.end) / 2
        raise ValueError(f"{self.kind} の平均値は定義されていません")
