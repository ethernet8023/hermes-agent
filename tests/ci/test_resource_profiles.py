"""Tests for resource profile loading + bottleneck classification in timings_report.py."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "timings_report.py"
_spec = importlib.util.spec_from_file_location("timings_report", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load timings_report.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_T0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _ts(seconds: float) -> str:
    dt = _T0.timestamp() + seconds
    return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _job(name: str, dur_s: float, start_s: float = 0.0, conclusion: str = "success") -> dict:
    return {
        "name": name,
        "duration_s": dur_s,
        "conclusion": conclusion,
        "started_at": _ts(start_s),
        "completed_at": _ts(start_s + dur_s),
        "wait_s": 0.0,
    }


def _timings(jobs: list[dict]) -> dict:
    return {"run_id": "123", "head_sha": "abc", "created_at": "", "jobs": jobs}


def _profile(label: str, cpu_avg: float = 50.0, cpu_peak: float = 80.0,
             mem_peak: float = 500.0, mem_total: float = 16000.0,
             disk_ops: float = 10.0, disk_mb: float = 5.0,
             duration_s: float = 60.0) -> dict:
    return {
        "label": label,
        "duration_s": duration_s,
        "cpu": {"avg_usage_pct": cpu_avg, "peak_usage_pct": cpu_peak, "samples": 60},
        "memory": {"avg_mb": mem_peak * 0.8, "peak_mb": mem_peak,
                   "total_mb": mem_total, "samples": 60},
        "disk": {"total_mb": disk_mb, "avg_ops_per_s": disk_ops,
                 "peak_ops_per_s": disk_ops * 2, "samples": 60},
    }


# ── load_resource_profiles ──────────────────────────────────────────────

def test_load_profiles_from_directory():
    with tempfile.TemporaryDirectory() as d:
        # Simulate artifact download structure: resource-profiles/<name>/resource-profile.json
        p1 = os.path.join(d, "resource-profile-tests-slice-1")
        os.makedirs(p1)
        with open(os.path.join(p1, "resource-profile.json"), "w") as f:
            json.dump(_profile("tests-slice-1"), f)

        p2 = os.path.join(d, "resource-profile-ruff-blocking")
        os.makedirs(p2)
        with open(os.path.join(p2, "resource-profile.json"), "w") as f:
            json.dump(_profile("ruff-blocking"), f)

        profiles = _mod.load_resource_profiles(d)
        assert len(profiles) == 2
        assert "tests-slice-1" in profiles
        assert "ruff-blocking" in profiles
        assert profiles["tests-slice-1"]["cpu"]["avg_usage_pct"] == 50.0


def test_load_profiles_empty_dir():
    with tempfile.TemporaryDirectory() as d:
        profiles = _mod.load_resource_profiles(d)
        assert profiles == {}


def test_load_profiles_nonexistent_dir():
    profiles = _mod.load_resource_profiles("/nonexistent/path/xyz")
    assert profiles == {}


def test_load_profiles_malformed_json_skipped():
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "resource-profile-bad")
        os.makedirs(p1)
        with open(os.path.join(p1, "resource-profile.json"), "w") as f:
            f.write("not json {{{")
        profiles = _mod.load_resource_profiles(d)
        assert profiles == {}


# ── classify_bottleneck ─────────────────────────────────────────────────

def test_bottleneck_no_profiles_evenly_distributed():
    """Multiple short jobs, no profiles → evenly distributed."""
    t = _timings([
        _job("a", 30.0),
        _job("b", 30.0, start_s=30.0),
        _job("c", 30.0, start_s=60.0),
    ])
    verdict = _mod.classify_bottleneck(t, {})
    assert "evenly distributed" in verdict.lower()


def test_bottleneck_cpu_bound():
    """A job with >80% avg CPU → CPU-bound."""
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, cpu_peak=99.0, duration_s=120.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "cpu" in verdict.lower()
    assert "compile" in verdict
    assert "95" in verdict


def test_bottleneck_disk_io_bound():
    """A job with >500 completed disk IOs/s → disk IO-bound."""
    t = _timings([_job("io-heavy", 60.0)])
    profiles = {"io-heavy": _profile("io-heavy", cpu_avg=30.0, disk_ops=900.0, disk_mb=500.0, duration_s=60.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "disk" in verdict.lower()
    assert "io-heavy" in verdict


def test_bottleneck_memory_bound():
    """A job using >85% of total memory → memory-bound."""
    t = _timings([_job("big-mem", 60.0)])
    profiles = {"big-mem": _profile("big-mem", cpu_avg=30.0, mem_peak=14500.0,
                                    mem_total=16000.0, duration_s=60.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "memory" in verdict.lower()
    assert "big-mem" in verdict
    assert "14500" in verdict


def test_bottleneck_memory_fallback_floor_without_total():
    """Profiles without total_mb fall back to a 16 GB floor."""
    t = _timings([_job("big-mem", 60.0)])
    p = _profile("big-mem", cpu_avg=30.0, mem_peak=15000.0, duration_s=60.0)
    del p["memory"]["total_mb"]
    verdict = _mod.classify_bottleneck(t, {"big-mem": p})
    assert "memory" in verdict.lower()


def test_bottleneck_moderate_used_memory_not_memory_bound():
    """Regression: moderate absolute usage on a big node must NOT flag as
    memory-bound (the old absolute >6000 MB threshold misfired on any
    box with lots of RAM)."""
    t = _timings([_job("normal", 60.0)])
    profiles = {"normal": _profile("normal", cpu_avg=95.0, cpu_peak=99.0,
                                   mem_peak=8000.0, mem_total=32000.0,
                                   duration_s=60.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "memory" not in verdict.lower()
    assert "cpu" in verdict.lower()


def test_bottleneck_wait_bound():
    """Total wait > 50% of wall time → wait-bound."""
    # Job A runs 0-10s, Job B waits 100s then runs 10s (wait=100s, wall=110s)
    t = _timings([
        _job("fast", 10.0, start_s=0.0),
        _job("delayed", 10.0, start_s=100.0),
    ])
    # Set wait_s on the delayed job
    t["jobs"][1]["wait_s"] = 100.0
    verdict = _mod.classify_bottleneck(t, {})
    assert "wait" in verdict.lower()


def test_bottleneck_serial_no_profiles():
    """No profiles, low parallelism, one job dominates → serial bottleneck."""
    t = _timings([
        _job("slow", 300.0, start_s=0.0),
        _job("fast1", 10.0, start_s=0.0),
        _job("fast2", 10.0, start_s=0.0),
    ])
    verdict = _mod.classify_bottleneck(t, {})
    assert "slow" in verdict.lower()


def test_bottleneck_no_jobs():
    """Empty timings → insufficient data."""
    t = _timings([])
    verdict = _mod.classify_bottleneck(t, {})
    assert "insufficient" in verdict.lower()


def test_bottleneck_cpu_wins_over_disk():
    """When both CPU and disk are high, CPU should win (higher sort key)."""
    t = _timings([_job("heavy", 120.0)])
    profiles = {"heavy": _profile("heavy", cpu_avg=95.0, disk_ops=600.0, duration_s=120.0)}
    verdict = _mod.classify_bottleneck(t, profiles)
    assert "cpu" in verdict.lower()


# ── generate_summary includes bottleneck ─────────────────────────────────

def test_summary_includes_bottleneck():
    t = _timings([_job("tests", 60.0)])
    summary = _mod.generate_summary(t, None)
    assert "Bottleneck:" in summary


def test_summary_includes_bottleneck_with_profiles():
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, duration_s=120.0)}
    summary = _mod.generate_summary(t, None, profiles)
    assert "Bottleneck:" in summary
    assert "CPU" in summary


# ── generate_review_status includes bottleneck ───────────────────────────

def test_review_status_includes_bottleneck():
    t = _timings([_job("tests", 60.0)])
    statuses = _mod.generate_review_status(t, None)
    result = statuses[0]["results"][0]
    assert "Bottleneck:" in result["summary"]


def test_review_status_includes_bottleneck_with_profiles():
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, duration_s=120.0)}
    statuses = _mod.generate_review_status(t, None, profiles=profiles)
    result = statuses[0]["results"][0]
    assert "Bottleneck:" in result["summary"]
    assert "CPU" in result["summary"]


# ── generate_html includes resource sections ─────────────────────────────

def test_html_includes_bottleneck_box():
    t = _timings([_job("tests", 60.0)])
    html = _mod.generate_html(t, None)
    assert "Bottleneck Analysis" in html


def test_html_includes_resource_table_with_profiles():
    t = _timings([_job("compile", 120.0)])
    profiles = {"compile": _profile("compile", cpu_avg=95.0, duration_s=120.0)}
    html = _mod.generate_html(t, None, profiles)
    assert "Resource Usage" in html
    assert "compile" in html
    assert "95%" in html


def test_html_no_resource_table_without_profiles():
    t = _timings([_job("tests", 60.0)])
    html = _mod.generate_html(t, None)
    assert "Resource Usage" not in html
