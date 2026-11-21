"""Public API. The CLI and any future GUI call these; nothing here prints or exits."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .audio.bpm import analyze_audio
from .config import Config, validate_for_render
from .effects.registry import validate_effects_config
from .errors import AnalysisError, ConfigError, NoValidInputError
from .media.inputs import InputReport, collect_input_paths, validate_inputs
from .media.output_format import resolve_output_format
from .media.probe import AudioInfo, ProbeError, probe_audio
from .media.tools import Tools, locate_tools
from .planner.plan import AudioSpec, Plan, load_plan, save_plan
from .planner.planner import build_plan, resolve_cut_mode
from .planner.rng import new_seed
from .render.ffmpeg import EncodeSettings
from .render.pipeline import render_plan as _render_plan
from .util.power import high_performance_power_plan
from .util.progress import ProgressCallback, null_progress

log = logging.getLogger("moviekaleidoscope")

__all__ = ["GenerateResult", "plan_path_for", "make_plan", "render_plan", "check_plan_files", "render_saved_plan", "generate",
           "load_plan"]


@dataclass
class GenerateResult:
    plan: Plan
    plan_path: Path
    output: Path | None                      # None for plan-only runs
    inputs: InputReport | None = None
    replaced_clips: list[int] = field(default_factory=list)


def plan_path_for(cfg: Config) -> Path:
    if cfg.plan is not None:
        return cfg.plan
    if cfg.output is None:
        raise ConfigError("plan の保存先を決められません(-o または --plan を指定してください)")
    return cfg.output.with_name(cfg.output.name + ".plan.json")


def _resolve_audio(cfg: Config, tools: Tools, info: AudioInfo | None, duration: float,
                   progress: ProgressCallback) -> tuple[str, AudioSpec | None]:
    """Decide the cut mode and the beat grid (SPEC §4.3).

    BPM comes from `--bpm` if given, else from analysing the music when the cut mode is `beat`.
    A failed analysis falls back to `fixed` for `auto`, and is an error for an explicit `beat`."""
    mode = resolve_cut_mode(cfg)
    bpm = offset = None
    if cfg.audio.bpm:
        bpm, offset = cfg.audio.bpm, cfg.audio.beat_offset or 0.0
    elif mode == "beat":
        if cfg.audio.file is None:
            raise ConfigError("beat 方式には音声(--audio)か BPM(--bpm)が必要です")
        progress("BPM 解析", 0, 1)
        try:
            # Look at the music itself, not just the part the video uses: a longer window is both
            # more reliable (see audio/bpm.py) and more precise.
            available = (info.duration - cfg.audio.start) if info is not None else duration
            grid = analyze_audio(tools, cfg.audio.file, cfg.audio.start, available, cfg.audio.bpm_range)
        except AnalysisError as e:
            if cfg.cut.mode != "auto":
                raise AnalysisError(f"{e}\n(--bpm で BPM を指定すると解析を省略できます)") from None
            log.warning("BPM を検出できないため、固定長のカットにします: %s(--bpm で指定できます)", e)
            mode = "fixed"
        else:
            bpm = grid.bpm
            offset = cfg.audio.beat_offset if cfg.audio.beat_offset is not None else grid.offset
            log.info("BPM %.3f / 最初の拍 %.3f 秒 / 確からしさ %.2f", bpm, offset, grid.confidence)
        progress("BPM 解析", 1, 1)
    if cfg.audio.file is None and bpm is None:
        return mode, None
    if info is not None and cfg.audio.start + duration > info.duration + 1e-3:
        log.warning("音声が出力より %.1f 秒短いため、残りは無音になります", cfg.audio.start + duration - info.duration)
    return mode, AudioSpec(cfg.audio.file, cfg.audio.start, bpm, offset)


def make_plan(cfg: Config, tools: Tools | None = None,
              progress: ProgressCallback = null_progress) -> tuple[Plan, InputReport]:
    """Collect and validate inputs and audio, then build the plan (no video is processed)."""
    validate_effects_config(cfg.effects)   # fail fast on bad effect names / ranges
    tools = tools or locate_tools(cfg.runtime.ffmpeg, cfg.runtime.ffprobe)
    info = None
    if cfg.audio.file is not None:
        try:
            info = probe_audio(cfg.audio.file, tools)
        except ProbeError as e:
            raise ConfigError(f"音声ファイルを使えません: {cfg.audio.file}({e})") from None
    if cfg.duration is not None:
        duration = cfg.duration
    elif info is not None:
        duration = info.duration - cfg.audio.start
        if duration <= 0:
            raise ConfigError(f"audio.start({cfg.audio.start} 秒)が音声の長さ({info.duration:.1f} 秒)以上です")
    else:
        raise ConfigError("出力の長さが指定されていません(--duration、または --audio で音声の長さを使用)")
    paths = collect_input_paths(cfg.inputs, cfg.recursive, cfg.extensions)
    report = validate_inputs(paths, tools, progress)
    fmt = resolve_output_format(cfg.video, report.valid[0])
    seed = cfg.seed if cfg.seed is not None else new_seed()
    mode, audio = _resolve_audio(cfg, tools, info, duration, progress)
    progress("計画", 0, 1)
    plan = build_plan(cfg, report.valid, fmt, seed, duration=duration, mode=mode, audio=audio)
    progress("計画", 1, 1)
    return plan, report


def check_plan_files(plan: Plan) -> None:
    """The input videos (and music) a saved plan refers to must still exist."""
    missing = sorted({str(c.source.path) for c in plan.clips if not c.source.path.is_file()})
    if missing:
        shown = ", ".join(missing[:3]) + (f" ほか {len(missing) - 3} 件" if len(missing) > 3 else "")
        raise NoValidInputError(f"plan が参照する入力動画が見つかりません: {shown}")
    if plan.audio is not None and plan.audio.path is not None and not plan.audio.path.is_file():
        raise ConfigError(f"plan が参照する音声ファイルが見つかりません: {plan.audio.path}")


def render_plan(plan: Plan, cfg: Config, output: Path, tools: Tools | None = None,
                progress: ProgressCallback = null_progress,
                cancel: threading.Event | None = None,
                inputs: InputReport | None = None) -> list[int]:
    """Render an existing plan. Encoding/runtime settings come from `cfg`.

    Pass `inputs` to allow one retry with a different source for failed clips.
    Returns the indices of clips whose source was replaced (plan is updated in place).
    """
    if inputs is None:                 # replay: nothing was validated in this run
        check_plan_files(plan)
    tools = tools or locate_tools(cfg.runtime.ffmpeg, cfg.runtime.ffprobe)
    with high_performance_power_plan(enabled=cfg.runtime.power_plan):
        return _render_plan(
            plan, tools=tools, enc=EncodeSettings(cfg.video.crf, cfg.video.preset, cfg.video.backend),
            runtime=cfg.runtime, output=output, progress=progress, cancel=cancel,
            inputs=None if inputs is None else inputs.valid,
        )


def render_saved_plan(plan_path: Path, cfg: Config,
                      progress: ProgressCallback = null_progress,
                      cancel: threading.Event | None = None) -> tuple[Plan, Path]:
    """Re-render a plan file into `cfg.output` (what `--from-plan` does).

    The plan fixes everything about the content; `cfg` supplies only the output path, the
    encoder settings and the runtime settings. Returns (plan, output)."""
    validate_for_render(cfg)
    plan = load_plan(plan_path)
    render_plan(plan, cfg, cfg.output, progress=progress, cancel=cancel)   # no `inputs`: failures are fatal
    return plan, cfg.output


def generate(cfg: Config, *, plan_only: bool = False,
             progress: ProgressCallback = null_progress,
             cancel: threading.Event | None = None) -> GenerateResult:
    """Inputs -> plan -> (unless plan_only) rendered video."""
    tools = locate_tools(cfg.runtime.ffmpeg, cfg.runtime.ffprobe)
    plan, report = make_plan(cfg, tools, progress)
    plan_path = plan_path_for(cfg)
    save_plan(plan, plan_path)
    log.info("seed=%d / クリップ %d 個 / plan: %s", plan.seed, len(plan.clips), plan_path)
    if plan_only:
        return GenerateResult(plan, plan_path, None, report)

    assert cfg.output is not None
    replaced = render_plan(plan, cfg, cfg.output, tools, progress, cancel, inputs=report)
    if replaced:
        save_plan(plan, plan_path)  # record the sources that were actually used
    return GenerateResult(plan, plan_path, cfg.output, report, replaced)
