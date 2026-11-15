"""Effect interface and parameter sampling (SPEC §6, §7).

An Effect is stateless. `sample` draws a plain-data parameter dict (stored in the plan);
`pre_filters` / `post_filters` turn that dict into ffmpeg filters at render time.
Nothing here draws random numbers at render time.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Mapping

import numpy as np

from ..planner.motion import Motion

# (frame RGB uint8 HxWx3, t seconds from clip start, n frame index) -> frame
FrameProcessor = Callable[[np.ndarray, float, int], np.ndarray]

ALL_MOTIONS = ("const", "ramp", "sine", "pulse")


def q(expr: str) -> str:
    """Quote an ffmpeg filter option value (expressions contain commas)."""
    if "'" in expr:
        raise ValueError("expression must not contain a single quote")
    return f"'{expr}'"


@dataclass(frozen=True)
class ParamSpec:
    """A continuous parameter: its default value range and which motions it may use."""

    name: str
    lo: float
    hi: float
    motions: tuple[str, ...] = ALL_MOTIONS
    log: bool = False          # sample log-uniformly (e.g. speed factors)
    doc: str = ""


@dataclass(frozen=True)
class SampleContext:
    duration: float            # clip length in seconds
    bpm: float | None = None


@dataclass(frozen=True)
class RenderContext:
    duration: float
    frames: int
    fps: Fraction
    width: int
    height: int
    bpm: float | None = None


def _r(x: float) -> float:
    return round(float(x), 4)


def sample_period(rng: random.Random, sctx: SampleContext, beats: tuple[int, ...],
                  secs: tuple[float, float]) -> tuple[float, str]:
    """A time length: in beats when the BPM is known, otherwise in seconds."""
    if sctx.bpm:
        return float(rng.choice(beats)), "beat"
    return _r(rng.uniform(*secs)), "sec"


def sample_value(rng: random.Random, lo: float, hi: float, log: bool = False) -> float:
    if log:
        return _r(math.exp(rng.uniform(math.log(lo), math.log(hi))))
    return _r(rng.uniform(lo, hi))


def sample_motion(rng: random.Random, spec: ParamSpec, lo: float, hi: float,
                  sctx: SampleContext) -> dict[str, Any]:
    """Pick a motion kind allowed for `spec` and draw its coefficients inside [lo, hi]."""
    kinds = [k for k in spec.motions if not (k == "pulse" and not sctx.bpm)]  # pulse needs a BPM
    kind = rng.choice(kinds)
    span = hi - lo
    if kind == "const":
        return Motion.const(sample_value(rng, lo, hi, spec.log)).to_dict()
    if kind == "ramp":
        a, b = sample_value(rng, lo, hi, spec.log), sample_value(rng, lo, hi, spec.log)
        return Motion("ramp", a, end=b).to_dict()
    if kind == "sine":
        amp = _r(rng.uniform(0.1, 0.4) * span)
        centre = _r(rng.uniform(lo + amp, hi - amp))
        period, unit = sample_period(rng, sctx, (1, 2, 4), (0.5, 2.0))
        return Motion("sine", centre, amp=amp, period=period, unit=unit,
                      phase=_r(rng.uniform(0, 2 * math.pi))).to_dict()
    base = _r(rng.uniform(lo, lo + span / 2))
    amp = _r(rng.uniform(span / 4, hi - base))
    period, unit = sample_period(rng, sctx, (1, 2), (0.4, 1.0))
    return Motion("pulse", base, amp=amp, period=period, unit=unit,
                  decay=_r(rng.uniform(2, 5))).to_dict()


class Effect:
    """Base class. Subclasses set the class attributes and override what they need."""

    name: str = ""
    description: str = ""
    backend: str = "ffmpeg"          # "ffmpeg" or "frame"
    params: tuple[ParamSpec, ...] = ()
    options: Mapping[str, float] = {}   # extra tunables: name -> default
    needs_input_bound: bool = False  # the filter reads its input to EOF (e.g. reverse)

    def available(self, sctx: SampleContext, opts: Mapping[str, float]) -> bool:
        return True

    def sample(self, rng: random.Random, sctx: SampleContext,
               ranges: Mapping[str, tuple[float, float]], opts: Mapping[str, float]) -> dict[str, Any]:
        raise NotImplementedError

    def source_span(self, params: Mapping[str, Any], duration: float) -> float:
        """Seconds of source needed for `duration` seconds of output."""
        return duration

    def backend_for(self, params: Mapping[str, Any]) -> str:
        """Backend for these parameters (an effect may need Python only for some of them)."""
        return self.backend

    def frame_processor(self, params: Mapping[str, Any], ctx: RenderContext) -> FrameProcessor:
        """Per-frame function for the frame backend. Called once per clip (may keep state).
        Must be cheap to construct: plans are validated by constructing it."""
        raise NotImplementedError(f"{self.name} has no frame implementation")

    def pre_filters(self, params: Mapping[str, Any], ctx: RenderContext) -> list[str]:
        """Filters applied before the fps conversion (time-axis effects)."""
        return []

    def post_filters(self, params: Mapping[str, Any], ctx: RenderContext) -> list[str]:
        """Filters applied after scale/crop (image effects)."""
        return []

    # ---- helpers ---------------------------------------------------------------
    def motion(self, params: Mapping[str, Any], key: str) -> Motion:
        return Motion.from_dict(params[key])

    def sample_params(self, rng: random.Random, sctx: SampleContext,
                      ranges: Mapping[str, tuple[float, float]], names: tuple[str, ...] | None = None) -> dict[str, Any]:
        """Sample a motion for each continuous parameter (all, or only `names`)."""
        out = {}
        for spec in self.params:
            if names is None or spec.name in names:
                lo, hi = ranges[spec.name]
                out[spec.name] = sample_motion(rng, spec, lo, hi, sctx)
        return out


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def value_fn(effect: Effect, params: Mapping[str, Any], key: str, ctx: RenderContext) -> Callable[[float], float]:
    """t -> value of the parameter's motion (for frame processors)."""
    m = effect.motion(params, key)
    duration, bpm = ctx.duration, ctx.bpm
    return lambda t: m.value(t, duration, bpm)


def phase_fn(effect: Effect, params: Mapping[str, Any], key: str, ctx: RenderContext) -> Callable[[float], float]:
    """t -> integral of a const/ramp rate over [0, t] (e.g. Hz -> cycles). Exact for both."""
    m = effect.motion(params, key)
    if m.kind == "const":
        return lambda t: m.base * t
    if m.kind == "ramp":
        a, b, d = m.base, m.end, ctx.duration
        # accelerate linearly over the clip, then hold the final rate
        return lambda t: a * min(t, d) + (b - a) * min(t, d) ** 2 / (2 * d) + b * max(0.0, t - d)
    raise ValueError(f"{key} は const か ramp のみ対応しています")
