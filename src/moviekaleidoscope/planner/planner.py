"""Planner: turn config + validated inputs into a Plan (SPEC §5).

Cut modes: `fixed`, `random`, and `beat` (boundaries on the beat grid of the music).
"""

from __future__ import annotations

import math
import random
from fractions import Fraction

from ..config import Config
from ..effects.base import SampleContext
from ..effects.registry import EffectPool
from ..errors import ConfigError
from ..media.output_format import OutputFormat
from ..media.probe import VideoInfo
from .plan import AudioSpec, ClipSpec, OutputSpec, Plan, SourceSpec
from .rng import make_rng

MIN_TAIL_FRAMES = 4      # a final clip shorter than this is merged into the previous one
SOURCE_MARGIN = 0.05     # seconds kept free at the end of a source file


def frames_at(t: Fraction, fps: Fraction) -> int:
    """Frame index of time t (round half up). Boundaries are always derived from
    cumulative times so that rounding errors never accumulate."""
    return math.floor(t * fps + Fraction(1, 2))


def resolve_cut_mode(cfg: Config) -> str:
    """`auto` becomes `beat` when there is music or a manual BPM, otherwise `fixed`."""
    if cfg.cut.mode == "auto":
        return "beat" if (cfg.audio.file or cfg.audio.bpm) else "fixed"
    return cfg.cut.mode


class _SourceBag:
    """Shuffle-bag: every input is used once per round, so inputs are treated equally."""

    def __init__(self, n: int, rng: random.Random) -> None:
        self._n, self._rng, self._bag = n, rng, []

    def next(self) -> int:
        if not self._bag:
            self._bag = list(range(self._n))
            self._rng.shuffle(self._bag)
        return self._bag.pop()


def place_source(info: VideoInfo, span: float, rng: random.Random) -> SourceSpec:
    """Random in-point for `span` seconds; loop from the start when the file is too short."""
    room = info.duration - span - SOURCE_MARGIN
    if room > 0:
        return SourceSpec(info.path, round(rng.uniform(0.0, room), 3), span, False, info.matrix)
    return SourceSpec(info.path, 0.0, span, True, info.matrix)


def reselect_source(plan: Plan, clip: ClipSpec, infos: list[VideoInfo]) -> SourceSpec | None:
    """Pick a different input for a clip whose rendering failed (SPEC §3.2)."""
    candidates = [i for i in infos if i.path != clip.source.path]
    if not candidates:
        return None
    rng = make_rng(plan.seed, f"retry:{clip.index}")
    return place_source(rng.choice(candidates), clip.source.span, rng)


def build_plan(cfg: Config, infos: list[VideoInfo], fmt: OutputFormat, seed: int, *,
               duration: float, mode: str, audio: AudioSpec | None = None) -> Plan:
    """`mode` is a concrete cut mode (fixed / random / beat). With `beat`, `audio.bpm` is required.

    `audio.bpm` (when known) also lets effects use beat-based timing, whatever the cut mode."""
    if not infos:
        raise ConfigError("入力がありません")
    bpm = audio.bpm if audio else None
    if mode == "beat" and not bpm:
        raise ConfigError("beat 方式には BPM が必要です")
    fps = fmt.fps
    total = frames_at(Fraction(duration).limit_denominator(1_000_000), fps)
    if total < 1:
        raise ConfigError("出力の長さが短すぎます(1 フレーム未満)")

    cut_rng = make_rng(seed, "cut")
    src_rng = make_rng(seed, "source")
    fx_rng = make_rng(seed, "effect")
    bag = _SourceBag(len(infos), src_rng)
    pool = EffectPool(cfg.effects)

    if mode == "beat":
        period = Fraction(60) / Fraction(bpm).limit_denominator(1_000_000)
        offset = Fraction(audio.beat_offset or 0.0).limit_denominator(1_000_000) % period
        # A lead-in before the first beat is its own clip if it is at least a quarter beat and
        # a few frames long; otherwise it is absorbed into the first clip.
        lead_in = offset >= period / 4 and frames_at(offset, fps) >= MIN_TAIL_FRAMES
        grid = offset                      # last boundary on the beat grid

    def next_end() -> Fraction:
        """beat: the absolute time at which the next clip ends (a point on the beat grid).
        fixed / random: the next clip's length; the caller accumulates it, so every boundary
        comes from a cumulative time and rounding never adds up."""
        nonlocal grid, lead_in
        if mode == "beat":
            if lead_in:
                lead_in = False
                return offset
            grid += cut_rng.choice(cfg.cut.beats) * period
            return grid
        length = cfg.cut.fixed if mode == "fixed" else cut_rng.uniform(cfg.cut.random_min, cfg.cut.random_max)
        return Fraction(length).limit_denominator(1_000_000)

    clips: list[ClipSpec] = []
    start, t = 0, Fraction(0)
    while start < total:
        while True:
            t = next_end() if mode == "beat" else t + next_end()
            end = min(frames_at(t, fps), total)
            if end > start:
                break
        if 0 < total - end < MIN_TAIL_FRAMES:
            end = total
        frames = end - start
        clip_duration = float(Fraction(frames) / fps)
        effect = pool.pick(fx_rng, SampleContext(clip_duration, bpm))
        span = round(pool.source_span(effect, clip_duration), 6)   # e.g. speed changes how much source is used
        # In-point from a per-clip stream: whether some clip loops (it draws no number) or how
        # long it needs to be (speed) must not shift the choices made for the other clips.
        source = place_source(infos[bag.next()], span, make_rng(seed, f"in:{len(clips)}"))
        clips.append(ClipSpec(len(clips), start, frames, source, [effect]))
        start = end

    plan = Plan(
        seed=seed, cut_mode=mode,
        output=OutputSpec(fmt.width, fmt.height, fps, round(float(Fraction(total) / fps), 6), total),
        clips=clips, audio=audio,
    )
    plan.validate()
    return plan
