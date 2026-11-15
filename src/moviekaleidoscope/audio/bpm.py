"""Tempo and beat phase of a steady-tempo track (SPEC §4.3), in plain numpy.

Why not librosa's beat_track: measured on exact click tracks it returned 117.45 for 120 BPM,
143.6 for 140, 129.2 for 128.3 (and first beats up to 0.4 s off). A 2 % tempo error is a 2.4 s
drift over a 2-minute analysis window, so cuts would no longer land on the beat.

Method: an onset-strength envelope (log-spectral flux) is folded modulo a candidate period; the
tempo whose fold is most concentrated (|sum w*exp(-2 pi i t/P)| / sum w) is the tempo, and the
phase of that sum is the beat position. The search is a fine grid, refined to 0.002 BPM.
Two bands are tried -- everything, and below 200 Hz (kick/bass) -- and the one that is more
periodic wins: with full-band flux a pattern of kicks on the beat and hats on the off-beat locks
on to the hats (the wrong half of the beat).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from ..errors import AnalysisError
from ..media.tools import Tools
from ..util.process import run_bytes

SAMPLE_RATE = 22050
N_FFT = 1024
HOP = 128
SPLIT_HZ = 200.0
# Tempo precision grows with the window (uncertainty ~ 1/window), and the beat grid is extrapolated
# over the whole video, so use plenty of music; the analysis itself is cheap (<1 s for 300 s).
MAX_ANALYSIS_SEC = 300.0
MIN_CONCENTRATION = 0.15     # below this there is no usable periodicity (noise, silence)
# The peak must beat the best chance peak (see _null_level) by this factor. Measured over 30
# random noise tracks per length: chance never exceeded 1.58x. Real beats (with timing jitter and
# a noise floor) stayed above 2.0x from 15 s on; at 12 s some dropped to 1.76x, which overlaps with
# chance, hence the minimum length below.
NULL_MARGIN = 1.7
MIN_ANALYSIS_SEC = 15.0      # shorter audio cannot be told apart from noise reliably
NULL_BLOCK = 8               # frames: shuffled in blocks, so that one onset stays one onset
# The flux peak appears ~16 ms before the onset's real position (frame-window geometry).
# Calibrated on synthetic click and kick tracks (measured -15.4 ms and -17.8..-19.0 ms).
LATENCY = 0.0165


@dataclass(frozen=True)
class BeatGrid:
    bpm: float
    offset: float          # seconds from the start of the analysed audio to the first beat, in [0, 60/bpm)
    confidence: float      # 0..1 concentration of the onsets on the grid


class BpmDetector(Protocol):
    def detect(self, samples: np.ndarray, sr: int, bpm_range: tuple[float, float]) -> BeatGrid: ...


def onset_envelopes(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(times, full-band onset strength, low-band onset strength), one value per STFT frame."""
    if len(y) < N_FFT * 4:
        raise AnalysisError("音声が短すぎて拍を検出できません")
    win = np.hanning(N_FFT).astype(np.float32)
    n = 1 + (len(y) - N_FFT) // HOP
    logs = []
    for s in range(0, n, 2048):                      # chunked: keeps memory small for long audio
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(s, min(n, s + 2048))[:, None]
        logs.append(np.log1p(100.0 * np.abs(np.fft.rfft(y[idx] * win, axis=1))))
    L = np.concatenate(logs)
    rise = np.maximum(0.0, L[1:] - L[:-1])           # positive spectral flux
    low_bins = max(2, int(SPLIT_HZ * N_FFT / sr))
    k = int(0.4 * sr / HOP) | 1

    def envelope(flux: np.ndarray) -> np.ndarray:
        flux = np.concatenate([[0.0], flux])
        return np.maximum(0.0, flux - np.convolve(flux, np.ones(k) / k, mode="same"))

    t = (np.arange(n) * HOP + N_FFT / 2) / sr
    return t, envelope(rise.sum(axis=1)), envelope(rise[:, :low_bins].sum(axis=1))


def _concentration(t: np.ndarray, w: np.ndarray, bpms: np.ndarray) -> np.ndarray:
    """|sum w e^{-2 pi i t / P}| / sum w for each tempo (1 = every onset on one phase)."""
    period = 60.0 / bpms
    out = np.empty(len(bpms))
    for s in range(0, len(bpms), 64):
        out[s:s + 64] = np.abs((w[None, :] * np.exp(-2j * np.pi * t[None, :] / period[s:s + 64, None])).sum(axis=1))
    return out / w.sum()


def _null_level(t: np.ndarray, w: np.ndarray, lo: float, hi: float) -> float:
    """Highest concentration that arrives by chance: the same onsets in shuffled order.

    With a short analysis window, some tempo out of thousands of candidates always lines up a
    little by accident -- even on pure noise. Only a peak well above this level is a beat."""
    rng = np.random.RandomState(0)
    blocks = [w[i:i + NULL_BLOCK] for i in range(0, len(w), NULL_BLOCK)]
    grid = np.arange(lo, hi + 1e-9, 0.25)
    levels = []
    for _ in range(2):
        shuffled = np.concatenate([blocks[i] for i in rng.permutation(len(blocks))])
        keep = shuffled > 0.1 * shuffled.max()
        levels.append(_concentration(t[keep], shuffled[keep], grid).max())
    return float(np.mean(levels))


def _prior(bpm: float) -> float:
    """Mild preference for tempi near 120 BPM; only breaks ties between octaves."""
    return math.exp(-0.5 * math.log2(bpm / 120.0) ** 2)


def _analyse_band(t: np.ndarray, w: np.ndarray, lo: float, hi: float, search_hi: float | None = None):
    """Best (bpm, phase, concentration, score, chance level) for one band, folded into [lo, hi].

    `search_hi` > `hi` looks for tempi above the range: such a track has its onsets on a grid
    that is only visible at the true tempo (at half the tempo they cancel, alternating between
    two opposite phases). The true tempo is then halved into the range; the beat phase stays
    valid, because every second beat of the fast grid is a beat of the slow one."""
    null = _null_level(t, w, lo, search_hi or hi)
    keep = w > 0.1 * w.max()                         # strong onsets only: faster and cleaner
    t, w = t[keep], w[keep]
    grid = np.arange(lo, (search_hi or hi) + 1e-9, 0.05)
    conc = _concentration(t, w, grid)
    peaks = [i for i in range(1, len(grid) - 1) if conc[i] >= conc[i - 1] and conc[i] > conc[i + 1]] or [int(np.argmax(conc))]
    best = max(peaks, key=lambda i: conc[i] * _prior(grid[i]))
    fine = np.arange(max(lo, grid[best] - 0.15), min(grid[-1], grid[best] + 0.15) + 1e-9, 0.002)
    bpm = float(fine[int(np.argmax(_concentration(t, w, fine)))])
    period = 60.0 / bpm
    z = (w * np.exp(-2j * np.pi * t / period)).sum()
    phase = float((-np.angle(z) / (2 * np.pi) * period) % period)
    while bpm > hi and bpm / 2 >= lo:                # fold down; phase (< one fast period) stays valid
        bpm /= 2
    return bpm, phase, float(abs(z) / w.sum()), float(conc[best] * _prior(grid[best])), null


class CombDetector:
    """Default detector: fine tempo grid + circular phase estimate."""

    def detect(self, samples: np.ndarray, sr: int, bpm_range: tuple[float, float]) -> BeatGrid:
        lo, hi = bpm_range
        if len(samples) / sr < MIN_ANALYSIS_SEC:
            raise AnalysisError(f"音声が短すぎて(解析できるのは {len(samples) / sr:.1f} 秒)拍を確実に検出できません"
                                f"({MIN_ANALYSIS_SEC:.0f} 秒以上が必要)")
        t, full, low = onset_envelopes(samples, sr)
        if full.max() <= 0:
            raise AnalysisError("無音のため拍を検出できません")
        bands = [full] + ([low] if low.max() > 0 else [])

        def best(search_hi: float | None):
            return max((_analyse_band(t, w, lo, hi, search_hi) for w in bands), key=lambda c: c[3])

        def usable(conc: float, null: float) -> bool:
            return conc >= MIN_CONCENTRATION and conc >= NULL_MARGIN * null

        bpm, phase, conc, _, null = best(None)
        if not usable(conc, null):
            # Nothing periodic inside the range. Faster-than-range music (e.g. 300 BPM) looks like
            # noise there, so look up to twice the range before giving up. (Only as a fallback: with
            # the wider range the 8th-note grid of ordinary music would win over its real beat.)
            bpm, phase, conc, _, null = best(min(hi * 2.0, 400.0))
        if not usable(conc, null):
            raise AnalysisError(f"拍の周期性を検出できませんでした(確からしさ {conc:.2f}、偶然の水準 {null:.2f})")
        return BeatGrid(bpm=bpm, offset=(phase + LATENCY) % (60.0 / bpm), confidence=conc)


def decode_mono(tools: Tools, path: Path, start: float, duration: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Mono float32 samples of audio[start : start+duration], decoded by ffmpeg."""
    cmd = [tools.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    try:
        rc, raw, err = run_bytes(cmd, timeout=120)
    except OSError as e:
        raise AnalysisError(f"音声をデコードできません: {e}") from None
    if rc != 0 or not raw:
        raise AnalysisError(f"音声をデコードできません: {err.strip().splitlines()[-1] if err.strip() else '出力なし'}")
    return np.frombuffer(raw, dtype="<f4")


def analyze_audio(tools: Tools, path: Path, start: float, duration: float,
                  bpm_range: tuple[float, float], detector: BpmDetector | None = None) -> BeatGrid:
    """Beat grid from the first `min(duration, 300 s)` of audio after `start` (steady tempo assumed).

    `duration` is how much music there is to look at (not how long the video is: a longer window
    gives a more reliable and more precise tempo). `offset` is relative to `start`, i.e. to t=0
    of the video."""
    samples = decode_mono(tools, path, start, min(duration, MAX_ANALYSIS_SEC))
    return (detector or CombDetector()).detect(samples, SAMPLE_RATE, bpm_range)
