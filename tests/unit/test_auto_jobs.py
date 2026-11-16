"""Default parallelism is lowered to fit in free memory (C5, 2026-11-16: 4K sources at the
CPU-based default of 12 jobs used 17 GB)."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from moviekaleidoscope.planner.plan import ClipSpec, EffectSpec, OutputSpec, Plan, SourceSpec
from moviekaleidoscope.render import pipeline
from moviekaleidoscope.render.pipeline import auto_jobs, job_memory_estimate
from moviekaleidoscope.util.memory import available_bytes

GB = 2**30
UHD, HD720 = 3840 * 2160, 1280 * 720


def plan(effects=("hue",), frames=60, w=1920, h=1080) -> Plan:
    clips = [ClipSpec(i, i * frames, frames, SourceSpec(Path("a.mp4"), 0.0, 2.0), [EffectSpec(e) for e in effects])
             for i in range(10)]
    n = len(clips) * frames
    return Plan(seed=1, cut_mode="fixed", output=OutputSpec(w, h, Fraction(30), n / 30, n), clips=clips)


@pytest.fixture(autouse=True)
def cpus(monkeypatch):
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 24)      # CPU default: 12 jobs


def test_estimate_matches_the_measurements():
    # measured at 12 jobs: 4K sources 17.2 GB (~1.4 GB each), 720p 2.1 GB (~0.17 GB each)
    assert 1.2 * GB <= job_memory_estimate(UHD, plan()) <= 1.8 * GB
    assert 0.15 * GB <= job_memory_estimate(HD720, plan()) <= 0.35 * GB


def test_plenty_of_memory_keeps_the_cpu_default():
    assert auto_jobs(HD720, plan(), 32 * GB) == 12


def test_4k_sources_on_16_gb_are_limited():
    jobs = auto_jobs(UHD, plan(), 12 * GB)         # 12 GB free on a 16 GB PC
    assert 1 <= jobs < 12
    assert jobs * job_memory_estimate(UHD, plan()) <= 0.7 * 12 * GB


def test_reverse_buffers_count():
    assert job_memory_estimate(HD720, plan(("reverse",), frames=300)) > job_memory_estimate(HD720, plan(frames=300)) + GB // 2


def test_never_below_one_and_unknown_memory_uses_cpu_default():
    assert auto_jobs(UHD, plan(), 100 * 2**20) == 1
    assert auto_jobs(UHD, plan(), None) == 12


def test_available_memory_is_readable_here():
    free = available_bytes()
    assert free is None or free > 0
