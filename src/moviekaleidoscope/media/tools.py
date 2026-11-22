"""Locate ffmpeg / ffprobe (SPEC §11).

Search order: explicit setting (config/CLI) -> FFMPEG_BIN / FFPROBE_BIN
environment variable -> (ffprobe only) next to the located ffmpeg -> PATH.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import ToolNotFoundError
from ..util.process import run_capture

log = logging.getLogger("moviekaleidoscope")

# Oldest ffmpeg this project is verified against (the development environment).
MIN_FFMPEG_VERSION = (4, 4)

_INSTALL_HINT = (
    "ffmpeg をインストールして PATH に追加するか、--ffmpeg / --ffprobe(または環境変数 "
    "FFMPEG_BIN / FFPROBE_BIN)でパスを指定してください。\n"
    "  Windows: winget install Gyan.FFmpeg\n"
    "  macOS:   brew install ffmpeg\n"
    "  Linux:   sudo apt install ffmpeg"
)


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str
    version: tuple[int, ...] | None
    version_text: str


def _resolve(value: str) -> str | None:
    """A path to an existing file, or a command name found on PATH."""
    p = Path(value).expanduser()
    if p.is_file():
        return str(p)
    return shutil.which(value)


def _find(name: str, explicit: str | None, env_var: str) -> str:
    for source, value in (("設定", explicit), (f"環境変数 {env_var}", os.environ.get(env_var))):
        if value:
            found = _resolve(value)
            if found is None:
                raise ToolNotFoundError(f"{name} が見つかりません({source}: {value})\n{_INSTALL_HINT}")
            return found
    return ""


def parse_version(text: str) -> tuple[int, ...] | None:
    """'ffmpeg version 4.4.2-...' / 'n6.1' -> (4, 4). Git builds ('N-12345-g...') -> None."""
    m = re.search(r"version\s+[nN]?(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


def locate_tools(ffmpeg: str | None = None, ffprobe: str | None = None) -> Tools:
    ff = _find("ffmpeg", ffmpeg, "FFMPEG_BIN") or shutil.which("ffmpeg")
    if not ff:
        raise ToolNotFoundError(f"ffmpeg が見つかりません\n{_INSTALL_HINT}")

    fp = _find("ffprobe", ffprobe, "FFPROBE_BIN")
    if not fp:
        sibling = Path(ff).with_name(Path(ff).name.replace("ffmpeg", "ffprobe"))
        fp = str(sibling) if sibling != Path(ff) and sibling.is_file() else (shutil.which("ffprobe") or "")
    if not fp:
        raise ToolNotFoundError(f"ffprobe が見つかりません\n{_INSTALL_HINT}")

    try:
        code, out, _ = run_capture([ff, "-hide_banner", "-version"], timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ToolNotFoundError(f"ffmpeg を実行できません: {ff} ({e})") from None
    if code != 0:
        raise ToolNotFoundError(f"ffmpeg を実行できません: {ff}")
    first_line = out.splitlines()[0] if out else ""
    version = parse_version(first_line)
    if version is None:
        log.warning("ffmpeg のバージョンを判別できません(%s)。動作は保証されません", first_line or "不明")
    elif version < MIN_FFMPEG_VERSION:
        need = ".".join(map(str, MIN_FFMPEG_VERSION))
        raise ToolNotFoundError(f"ffmpeg {need} 以上が必要です(検出: {first_line})\n{_INSTALL_HINT}")
    return Tools(ffmpeg=ff, ffprobe=fp, version=version, version_text=first_line)
