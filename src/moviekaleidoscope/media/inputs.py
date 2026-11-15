"""Input collection and validation (SPEC §3.1, §3.2)."""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..errors import ConfigError, NoValidInputError
from ..util.progress import ProgressCallback, null_progress
from .probe import ProbeError, VideoInfo, probe_video
from .tools import Tools

log = logging.getLogger("moviekaleidoscope")

MIN_INPUT_DURATION = 0.5  # seconds; shorter inputs are skipped


def _natural_key(p: Path) -> list:
    parts: list = []
    for part in p.parts:
        parts.extend(
            (0, int(tok)) if tok.isdigit() else (1, tok.casefold())
            for tok in re.split(r"(\d+)", part) if tok
        )
    return parts


def _identity(p: Path) -> str:
    return os.path.normcase(os.path.realpath(p))


def collect_input_paths(inputs: Iterable[Path], recursive: bool,
                        extensions: Iterable[str]) -> list[Path]:
    """Expand files/folders into an ordered, de-duplicated list of file paths.

    Order: `inputs` order; inside a folder, natural (name) order.
    Directly given files are accepted regardless of extension.
    """
    exts = {e.lower().lstrip(".") for e in extensions}
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = _identity(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    inputs = list(inputs)
    if not inputs:
        raise ConfigError("入力が指定されていません")
    for item in inputs:
        if item.is_dir():
            walker = item.rglob("*") if recursive else item.iterdir()
            found = [
                f for f in walker
                if f.is_file() and not f.name.startswith(".")
                and f.suffix.lower().lstrip(".") in exts
            ]
            if not found:
                log.warning("フォルダに対象の動画がありません: %s", item)
            for f in sorted(found, key=lambda f: _natural_key(f.relative_to(item))):
                add(f)
        elif item.is_file():
            add(item)
        else:
            raise ConfigError(f"入力が見つかりません: {item}")
    return out


@dataclass
class InputReport:
    valid: list[VideoInfo] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)


def validate_inputs(paths: list[Path], tools: Tools,
                    progress: ProgressCallback = null_progress) -> InputReport:
    """Probe every input; skip unusable ones with a warning.

    Raises NoValidInputError when nothing usable remains.
    """
    report = InputReport()

    def check(p: Path) -> VideoInfo | str:
        try:
            info = probe_video(p, tools)
        except ProbeError as e:
            return str(e)
        if info.duration < MIN_INPUT_DURATION:
            return f"短すぎます({info.duration:.2f} 秒 < {MIN_INPUT_DURATION} 秒)"
        return info

    total = len(paths)
    progress("入力検証", 0, total)
    with ThreadPoolExecutor(max_workers=max(1, min(4, total))) as pool:
        for i, (p, result) in enumerate(zip(paths, pool.map(check, paths)), start=1):
            if isinstance(result, str):
                log.warning("入力をスキップします: %s(%s)", p, result)
                report.skipped.append((p, result))
            else:
                report.valid.append(result)
            progress("入力検証", i, total)

    if not report.valid:
        raise NoValidInputError("有効な入力動画がありません")
    return report
