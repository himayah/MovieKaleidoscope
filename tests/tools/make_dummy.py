"""Generate dummy media for tests (SPEC §13.1). Nothing is committed to the repo.

Usage (manual): python tests/tools/make_dummy.py OUTDIR
"""

from __future__ import annotations

import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from moviekaleidoscope.media.tools import Tools, locate_tools


@dataclass(frozen=True)
class DummySet:
    root: Path
    base: Path          # 1280x720 / 30fps / 10 s
    small43: Path       # 640x480 / 24fps / 8 s
    vertical: Path      # 1080x1920 / 30fps / 6 s
    ntsc: Path          # 640x360 / 30000/1001 fps / 5 s
    short: Path         # 1 s (needs looping)
    tiny: Path          # 0.3 s (below the 0.5 s minimum)
    rotated: Path       # 320x240 coded, rotate=90 metadata -> displays as 240x320
    truncated: Path     # head of a valid mp4 (no moov)
    empty: Path         # 0 bytes
    not_video: Path     # text with an .mp4 extension
    japanese: Path      # file in a folder with Japanese + spaces in its names
    click_wav: Path     # click track
    click_bpm: float
    click_offset: float


def _ffmpeg(tools: Tools, args: list[str]) -> None:
    cmd = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
    cp = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {cp.stderr.decode('utf-8', 'replace')}")


def make_video(tools: Tools, path: Path, *, size: str, rate: str, duration: float,
               source: str = "testsrc2", extra: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(tools, [
        "-f", "lavfi", "-i", f"{source}=size={size}:rate={rate}:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
        *(extra or []), str(path),
    ])
    return path


def make_click_wav(path: Path, *, bpm: float = 120.0, offset: float = 0.25,
                   duration: float = 30.0, sr: int = 22050) -> Path:
    """Mono 16-bit WAV with a click on every beat; first beat at `offset` seconds."""
    n = int(duration * sr)
    x = np.zeros(n)
    t = np.arange(int(0.03 * sr)) / sr
    click = np.sin(2 * np.pi * 1000 * t) * np.exp(-t / 0.008)
    beat = 60.0 / bpm
    k = 0
    while True:
        pos = int(round((offset + k * beat) * sr))
        if pos >= n:
            break
        seg = min(len(click), n - pos)
        x[pos:pos + seg] += click[:seg]
        k += 1
    pcm = (np.clip(x, -1, 1) * 0.8 * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def beat_signal(bpm: float, offset: float, duration: float, *, sr: int = 22050, kind: str = "click",
                jitter: float = 0.0, noise: float = 0.0, seed: int = 1) -> np.ndarray:
    """Mono float32 signal with a beat at offset + k*60/bpm.

    kind "click": a short 1 kHz click on every beat.
    kind "house": kick on every beat, snare noise on every second beat, hi-hat on every off-beat
                  (the hats are broadband, so a naive full-band onset detector locks on to them).
    jitter: std-dev (seconds) of random timing error per hit; noise: level of a white-noise bed.
    """
    rng = np.random.RandomState(seed)
    n = int(duration * sr)
    x = np.zeros(n)
    t = np.arange(int(0.03 * sr)) / sr
    click = np.sin(2 * np.pi * 1000 * t) * np.exp(-t / 0.008)
    kick = np.sin(2 * np.pi * (60 + 120 * np.exp(-t / 0.01)) * t) * np.exp(-t / 0.012)
    hat = rng.randn(len(t)) * np.exp(-t / 0.004) * 0.4
    snare = rng.randn(len(t)) * np.exp(-t / 0.02)

    def add(at: float, shape: np.ndarray, gain: float = 1.0) -> None:
        i = int(round((at + (rng.randn() * jitter if jitter else 0.0)) * sr))
        if 0 <= i < n:
            m = min(len(shape), n - i)
            x[i:i + m] += gain * shape[:m]

    period = 60.0 / bpm
    k = 0
    while offset + k * period < duration:
        at = offset + k * period
        if kind == "click":
            add(at, click)
        else:
            add(at, kick)
            add(at + period / 2, hat, 0.5)
            if k % 2 == 1:
                add(at, snare, 0.6)
        k += 1
    if noise:
        x += rng.randn(n) * noise
    return (np.clip(x, -1.0, 1.0) * 0.5).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, sr: int = 22050) -> Path:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def make_solid_video(tools: Tools, path: Path, color: str, *, size: str = "320x180", rate: str = "30",
                     duration: float = 10) -> Path:
    """A video of one flat colour: every cut between different colours is unmistakable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(tools, ["-f", "lavfi", "-i", f"color=c={color}:s={size}:r={rate}:d={duration}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", str(path)])
    return path


def build_all(root: Path, tools: Tools) -> DummySet:
    root.mkdir(parents=True, exist_ok=True)
    base = make_video(tools, root / "base_720p30.mp4", size="1280x720", rate="30", duration=10)
    small43 = make_video(tools, root / "small43_480p24.mp4", size="640x480", rate="24", duration=8)
    vertical = make_video(tools, root / "vertical_1080x1920.mp4", size="1080x1920", rate="30", duration=6)
    ntsc = make_video(tools, root / "ntsc_2997.mp4", size="640x360", rate="30000/1001", duration=5)
    short = make_video(tools, root / "short_1s.mp4", size="320x180", rate="30", duration=1)
    tiny = make_video(tools, root / "tiny_0p3s.mp4", size="320x180", rate="30", duration=0.3)
    # ffmpeg 4.4 only writes the display matrix when remuxing an existing file.
    unrotated = make_video(tools, root / "_unrotated.mp4", size="320x240", rate="30", duration=2)
    rotated = root / "rotated.mp4"
    # The `rotate` tag is only honoured by old ffmpeg (4.4 turns it into a display matrix; 8.1
    # writes nothing). Newer versions have -display_rotation (an input option) instead.
    try:
        _ffmpeg(tools, ["-display_rotation:v:0", "90", "-i", str(unrotated), "-c", "copy", str(rotated)])
    except RuntimeError:
        _ffmpeg(tools, ["-i", str(unrotated), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rotated)])
    unrotated.unlink()

    truncated = root / "truncated.mp4"
    truncated.write_bytes(base.read_bytes()[:4096])
    empty = root / "empty.mp4"
    empty.write_bytes(b"")
    not_video = root / "not_video.mp4"
    not_video.write_text("this is not a video\n", encoding="utf-8")

    jp_dir = root / "日本語 フォルダ"
    jp_dir.mkdir(exist_ok=True)
    japanese = jp_dir / "テスト 動画.mp4"
    japanese.write_bytes(short.read_bytes())

    bpm, offset = 120.0, 0.25
    click = make_click_wav(root / "click_120bpm.wav", bpm=bpm, offset=offset)
    return DummySet(root, base, small43, vertical, ntsc, short, tiny, rotated,
                    truncated, empty, not_video, japanese, click, bpm, offset)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_dummy.py OUTDIR")
    ds = build_all(Path(sys.argv[1]), locate_tools())
    print(f"dummy media written to {ds.root}")
