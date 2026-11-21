"""high_performance_power_plan() (GPU redesign SPEC §C)."""

from __future__ import annotations

import pytest

from moviekaleidoscope.util import power


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"


def _fake_run(get_stdout: bytes):
    def run(args, **kw):
        if args[1] == "/getactivescheme":
            return FakeCompleted(0, stdout=get_stdout)
        return FakeCompleted(0)
    return run


def test_switches_and_restores_on_success(monkeypatch):
    monkeypatch.setattr(power.os, "name", "nt")
    calls = []
    real_run = _fake_run(f"電源設定の GUID: {BALANCED}  (バランス)".encode("utf-8"))
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: calls.append(a[0]) or real_run(a[0], **k))

    with power.high_performance_power_plan():
        pass

    sets = [c[2] for c in calls if c[1] == "/s"]
    assert sets == [power.HIGH_PERFORMANCE_GUID, BALANCED]


def test_restores_even_on_exception(monkeypatch):
    monkeypatch.setattr(power.os, "name", "nt")
    calls = []
    other = "aaaaaaaa-0000-0000-0000-000000000000"
    real_run = _fake_run(f"GUID: {other}".encode("utf-8"))
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: calls.append(a[0]) or real_run(a[0], **k))

    with pytest.raises(ValueError):
        with power.high_performance_power_plan():
            raise ValueError("boom")

    sets = [c[2] for c in calls if c[1] == "/s"]
    assert sets == [power.HIGH_PERFORMANCE_GUID, other]


def test_no_switch_needed_when_already_high_performance(monkeypatch):
    monkeypatch.setattr(power.os, "name", "nt")
    calls = []
    real_run = _fake_run(f"GUID: {power.HIGH_PERFORMANCE_GUID}".encode("utf-8"))
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: calls.append(a[0]) or real_run(a[0], **k))

    with power.high_performance_power_plan():
        pass

    assert [c for c in calls if c[1] == "/s"] == []    # already there: nothing to switch or restore


def test_powercfg_missing_does_not_raise(monkeypatch):
    monkeypatch.setattr(power.os, "name", "nt")
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no powercfg")))
    with power.high_performance_power_plan():
        pass    # must not raise


def test_set_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(power.os, "name", "nt")

    def run(args, **kw):
        if args[1] == "/getactivescheme":
            return FakeCompleted(0, stdout=f"GUID: {BALANCED}".encode("utf-8"))
        return FakeCompleted(1, stderr=b"access denied")

    monkeypatch.setattr(power.subprocess, "run", run)
    with power.high_performance_power_plan():
        pass    # must not raise even though /s fails both times


def test_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(power.os, "name", "nt")
    calls = []
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: calls.append(a) or FakeCompleted(0))
    with power.high_performance_power_plan(enabled=False):
        pass
    assert calls == []


def test_noop_on_non_windows(monkeypatch):
    monkeypatch.setattr(power.os, "name", "posix")
    calls = []
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: calls.append(a) or FakeCompleted(0))
    with power.high_performance_power_plan():
        pass
    assert calls == []
