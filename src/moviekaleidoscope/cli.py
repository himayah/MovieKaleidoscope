"""Thin CLI wrapper (SPEC §10). All logic lives in the core modules."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .api import generate, render_saved_plan
from .effects.registry import describe
from .config import CUT_MODES, Config, build_config, validate_for_run
from .errors import ConfigError, MkError
from .util.progress import LogProgress, TqdmProgress, null_progress

log = logging.getLogger("moviekaleidoscope")

# Options that decide *what* the video contains; a plan already fixes them.
_PLAN_FIXED_OPTIONS = (
    ("input", "-i"), ("recursive", "--recursive"), ("duration", "-d"), ("seed", "--seed"),
    ("audio", "--audio"), ("no_audio", "--no-audio"), ("bpm", "--bpm"),
    ("cut_mode", "--cut-mode"), ("cut_length", "--cut-length"), ("cut_random", "--cut-random"),
    ("beats", "--beats"), ("width", "--width"), ("height", "--height"), ("fps", "--fps"),
    ("effects", "--effects"), ("exclude_effects", "--exclude-effects"), ("plan", "--plan"),
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mkaleido",
        description="動画を切り貼りしてサイケデリックな動画を生成する",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", metavar="FILE", help="設定ファイル(YAML)")
    p.add_argument("-i", "--input", action="append", metavar="PATH",
                   help="入力ファイルまたはフォルダ(複数回指定可)")
    p.add_argument("--recursive", action="store_true", default=None,
                   help="フォルダを再帰的に探索する")
    p.add_argument("-o", "--output", metavar="FILE", help="出力ファイル(.mp4)")
    p.add_argument("--plan", metavar="FILE", help="plan の保存先(既定 <output>.plan.json)")
    p.add_argument("--plan-only", action="store_true", help="plan だけ作って終了する(映像は処理しない)")
    p.add_argument("--from-plan", metavar="FILE", help="保存した plan から映像を作り直す")
    p.add_argument("-d", "--duration", metavar="T", help="出力の長さ(秒 または mm:ss)")
    p.add_argument("-y", "--overwrite", action="store_true", default=None,
                   help="既存の出力・plan を上書きする")
    p.add_argument("--seed", type=int, metavar="N", help="乱数シード")

    g = p.add_mutually_exclusive_group()
    g.add_argument("--audio", metavar="FILE", help="合成する音声ファイル")
    g.add_argument("--no-audio", action="store_true", help="音声なしにする")
    p.add_argument("--bpm", type=float, metavar="N", help="BPM を手動指定")

    p.add_argument("--cut-mode", choices=CUT_MODES, help="カット方式")
    p.add_argument("--cut-length", type=float, metavar="SEC", help="断片の長さを固定秒数にする")
    p.add_argument("--cut-random", metavar="MIN:MAX", help="断片の長さを秒の範囲でランダムにする")
    p.add_argument("--beats", metavar="N[,N...]", help="断片の長さを拍数で指定する(複数ならランダム)")

    p.add_argument("--width", type=int, metavar="N")
    p.add_argument("--height", type=int, metavar="N")
    p.add_argument("--fps", metavar="N", help="例: 30, 29.97, 30000/1001")
    p.add_argument("--effects", metavar="NAME,...", help="使うエフェクトを絞り込む")
    p.add_argument("--exclude-effects", metavar="NAME,...", help="使わないエフェクト")
    p.add_argument("--list-effects", action="store_true", help="エフェクトとパラメータの範囲を表示して終了")

    p.add_argument("--jobs", type=int, metavar="N", help="並列数(既定: 自動)")
    p.add_argument("--work-dir", metavar="DIR")
    p.add_argument("--keep-temp", action="store_true", default=None)
    p.add_argument("--ffmpeg", metavar="PATH")
    p.add_argument("--ffprobe", metavar="PATH")

    p.add_argument("--dump-config", action="store_true", help="解決後の設定を表示して終了")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed CLI arguments into a nested override dict (only given keys)."""
    o: dict[str, Any] = {}

    def put(path: str, value: Any) -> None:
        d = o
        keys = path.split(".")
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    if args.input:
        put("inputs", list(args.input))
    for flag, key in (("recursive", "recursive"), ("overwrite", "overwrite")):
        if getattr(args, flag):
            put(key, True)
    if args.output:
        put("output", args.output)
    if args.plan:
        put("plan", args.plan)
    if args.duration is not None:
        put("duration", args.duration)
    if args.seed is not None:
        put("seed", args.seed)

    if args.audio:
        put("audio.file", args.audio)
    if args.no_audio:
        put("audio.file", None)
    if args.bpm is not None:
        put("audio.bpm", args.bpm)

    # Length options imply a cut mode; conflicting combinations are errors.
    implied: dict[str, tuple[str, Any]] = {}
    if args.cut_length is not None:
        implied["fixed"] = ("cut.fixed", args.cut_length)
    if args.cut_random is not None:
        try:
            lo, hi = (float(x) for x in args.cut_random.split(":"))
        except ValueError:
            raise ConfigError(f"--cut-random は MIN:MAX の形式で指定してください(値: {args.cut_random!r})") from None
        implied["random"] = ("cut.random", {"min": lo, "max": hi})
    if args.beats is not None:
        try:
            beats = [int(x) for x in _split(args.beats)]
        except ValueError:
            raise ConfigError(f"--beats は整数のカンマ区切りで指定してください(値: {args.beats!r})") from None
        implied["beat"] = ("cut.beats", beats)
    if len(implied) > 1:
        raise ConfigError("--cut-length / --cut-random / --beats は同時に指定できません")
    if implied:
        (mode, (key, value)), = implied.items()
        if args.cut_mode and args.cut_mode != mode:
            raise ConfigError(f"--cut-mode {args.cut_mode} と矛盾する長さ指定です(指定された長さは {mode} 用)")
        put("cut.mode", mode)
        put(key, value)
    elif args.cut_mode:
        put("cut.mode", args.cut_mode)

    for name in ("width", "height"):
        if getattr(args, name) is not None:
            put(f"video.{name}", getattr(args, name))
    if args.fps is not None:
        put("video.fps", args.fps)
    if args.effects:
        put("effects.enabled", _split(args.effects))
    if args.exclude_effects:
        put("effects.exclude", _split(args.exclude_effects))

    if args.jobs is not None:
        put("runtime.jobs", args.jobs)
    if args.work_dir:
        put("runtime.work_dir", args.work_dir)
    if args.keep_temp:
        put("runtime.keep_temp", True)
    if args.ffmpeg:
        put("runtime.ffmpeg", args.ffmpeg)
    if args.ffprobe:
        put("runtime.ffprobe", args.ffprobe)
    return o


def dump_config(cfg: Config) -> str:
    return yaml.safe_dump(cfg.to_file_dict(), allow_unicode=True, sort_keys=False)


def _setup_logging(quiet: bool, verbose: bool) -> None:
    level = logging.ERROR if quiet else logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


def _check_from_plan_options(args: argparse.Namespace) -> None:
    given = [flag for attr, flag in _PLAN_FIXED_OPTIONS if getattr(args, attr, None)]
    if args.plan_only:
        given.append("--plan-only")
    if given:
        raise ConfigError(f"--from-plan と同時に指定できないオプションです: {', '.join(given)}"
                          "(変更できるのは -o、-y、-q/-v、--jobs などの実行設定と、設定ファイルのエンコード設定のみ)")


def _run_from_plan(args: argparse.Namespace, cfg: Config, progress) -> int:
    plan, output = render_saved_plan(Path(args.from_plan), cfg, progress)
    log.info("plan: seed=%d / クリップ %d 個", plan.seed, len(plan.clips))
    log.info("出力しました: %s", output)
    return 0


def _prepare_streams() -> None:
    """Never die on a character the console cannot show.

    Messages are Japanese. A console shows them (Windows uses the Unicode API), but redirected
    output uses the locale encoding -- cp932 on Japanese Windows cannot encode e.g. U+301C, and
    a failing print would turn a good run into a traceback."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _choose_progress(quiet: bool):
    if quiet:
        return null_progress
    return TqdmProgress() if sys.stderr.isatty() else LogProgress(log)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _prepare_streams()
    _setup_logging(args.quiet, args.verbose)
    progress = _choose_progress(args.quiet)
    try:
        if args.list_effects:
            sys.stdout.write(describe())
            return 0
        if args.from_plan:
            _check_from_plan_options(args)
        cfg = build_config(args.config, overrides_from_args(args), Path.cwd())
        if args.dump_config:
            sys.stdout.write(dump_config(cfg))
            return 0
        if args.from_plan:
            return _run_from_plan(args, cfg, progress)

        validate_for_run(cfg, plan_only=args.plan_only)
        result = generate(cfg, plan_only=args.plan_only, progress=progress)
        if result.inputs and result.inputs.skipped:
            log.info("スキップした入力: %d 件", len(result.inputs.skipped))
        if result.replaced_clips:
            log.warning("素材を差し替えたクリップ: %s(plan に反映済み)", result.replaced_clips)
        if result.output is not None:
            log.info("出力しました: %s", result.output)
        return 0
    except MkError as e:
        log.error("%s", e)
        return e.exit_code
    except KeyboardInterrupt:
        log.error("中断しました")
        return 130
    except OSError as e:          # unwritable output, full disk, vanished file, ...
        log.error("ファイル操作に失敗しました: %s", e)
        if args.verbose:
            log.exception("詳細")
        return 1
    except Exception as e:        # a bug: say so plainly instead of a bare traceback
        log.error("予期しないエラー: %s: %s(詳細は -v で表示。再現するなら plan と一緒に報告してください)",
                  type(e).__name__, e)
        if args.verbose:
            log.exception("詳細")
        return 1
    finally:
        if isinstance(progress, TqdmProgress):
            progress.close()


if __name__ == "__main__":
    sys.exit(main())
