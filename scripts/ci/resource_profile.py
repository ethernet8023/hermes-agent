#!/usr/bin/env python3
"""CPU / RAM / disk-IO profiler for CI jobs.

Runs as a background daemon: samples /proc every second, accumulates
stats, and on SIGTERM (or timeout) writes a JSON summary to the output
path. Pure stdlib — runs on the bare runner Python with zero deps.

Usage:
    python3 scripts/ci/resource_profile.py \\
        --output resource-profile.json \\
        --label "tests slice 1/8"

The composite action (.github/actions/profile) starts this as a
background process, runs the real command, then signals it to stop.

Output JSON shape:
    {
        "label": "tests slice 1/8",
        "duration_s": 42.3,
        "cpu": {
            "avg_usage_pct": 55.2,
            "peak_usage_pct": 89.1,
            "samples": 42
        },
        "memory": {                  # USED memory (MemTotal - MemAvailable)
            "avg_mb": 512.0,
            "peak_mb": 684.3,
            "samples": 42
        },
        "disk": {
            "total_mb": 12.4,        # sectors read+written, whole devices only
            "avg_ops_per_s": 5.2,    # completed read+write IOs per second
            "peak_ops_per_s": 20.1,
            "samples": 42
        }
    }

Caveat: /proc/stat, /proc/meminfo, and /proc/diskstats are NODE-wide.
Inside a Kubernetes pod these numbers include neighbor pods sharing the
node — treat them as indicative, not exact per-job attribution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time

_SAMPLE_INTERVAL_S = 1.0
_CLK_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PROC_STAT = "/proc/stat"
_PROC_MEMINFO = "/proc/meminfo"
_PROC_DISKSTATS = "/proc/diskstats"


def _read_cpu_usage(prev: dict | None) -> tuple[float, dict]:
    """Return (usage_pct since prev sample, current_jiffies_dict).

    Reads /proc/stat line 1 (aggregate CPU). usage_pct = non-idle / total.
    """
    try:
        with open(_PROC_STAT, encoding="ascii") as f:
            first_line = f.readline()
    except OSError:
        return (0.0, prev or {})

    parts = first_line.split()
    if len(parts) < 5:
        return (0.0, prev or {})

    # user, nice, system, idle, iowait, irq, softirq, steal, ...
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    cur = {"total": total, "idle": idle}

    if prev and cur["total"] != prev["total"]:
        d_total = cur["total"] - prev["total"]
        d_idle = cur["idle"] - prev["idle"]
        if d_total > 0:
            return (max(0.0, (1.0 - d_idle / d_total) * 100.0), cur)

    return (0.0, cur)


def _read_mem_mb() -> float:
    """Return *used* memory in MB (MemTotal - MemAvailable) from /proc/meminfo.

    Falls back to MemTotal - MemFree on old kernels without MemAvailable.
    Note: in a container this is the host/node view, not the cgroup view —
    numbers can include neighbor pods on shared nodes.
    """
    try:
        with open(_PROC_MEMINFO, encoding="ascii") as f:
            text = f.read()
    except OSError:
        return 0.0

    total_kb = 0
    available_kb = -1
    free_kb = -1
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available_kb = int(line.split()[1])
        elif line.startswith("MemFree:"):
            free_kb = int(line.split()[1])

    if total_kb <= 0:
        return 0.0
    unused_kb = available_kb if available_kb >= 0 else max(free_kb, 0)
    return max(0, total_kb - unused_kb) / 1024.0


def _read_diskstats() -> dict[str, tuple[int, int]]:
    """Return {device: (io_ops_completed, sectors_read_plus_written)}.

    We track whole block devices only (skip partitions — track sda not
    sda1, nvme0n1 not nvme0n1p1) so IO isn't double-counted. Each
    diskstats line:
    major minor name reads_completed reads_merged sectors_read time_reading
    writes_completed writes_merged sectors_written ...
    """
    try:
        with open(_PROC_DISKSTATS, encoding="ascii") as f:
            lines = f.readlines()
    except OSError:
        return {}

    result = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        # Skip virtual/removable devices
        if name.startswith(("loop", "ram", "sr")):
            continue
        # Skip partitions: sda1, vda2, mmcblk0p1, nvme0n1p1. For devices
        # whose base name ends in a digit (nvme0n1, mmcblk0), partitions
        # carry a 'p<N>' suffix; for sdX/vdX a bare trailing digit.
        if name.startswith(("nvme", "mmcblk")):
            if re.search(r"p\d+$", name):
                continue
        elif name[-1].isdigit():
            continue
        reads_completed = int(parts[3])
        writes_completed = int(parts[7])
        sectors = int(parts[5]) + int(parts[9])
        result[name] = (reads_completed + writes_completed, sectors)

    return result


def _read_mem_total_mb() -> float:
    """Return MemTotal in MB (0.0 if unreadable)."""
    try:
        with open(_PROC_MEMINFO, encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def run_profiler(output_path: str, label: str, timeout_s: float = 0) -> None:
    """Sample resources until SIGTERM or timeout, then write JSON summary."""
    cpu_samples: list[float] = []
    mem_samples: list[float] = []
    disk_prev = _read_diskstats()
    disk_total_sectors = 0
    disk_ops_samples: list[float] = []

    cpu_prev: dict | None = None
    start = time.monotonic()
    running = [True]  # mutable for signal handler

    def _stop(*_):
        running[0] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running[0]:
        cpu_pct, cpu_prev = _read_cpu_usage(cpu_prev)
        cpu_samples.append(cpu_pct)

        mem_mb = _read_mem_mb()
        mem_samples.append(mem_mb)

        disk_cur = _read_diskstats()
        delta_sectors = 0
        delta_ops = 0
        for dev, (ops, sectors) in disk_cur.items():
            prev_ops, prev_sectors = disk_prev.get(dev, (ops, sectors))
            delta_sectors += max(0, sectors - prev_sectors)
            delta_ops += max(0, ops - prev_ops)
        disk_total_sectors += delta_sectors
        disk_ops_samples.append(delta_ops / _SAMPLE_INTERVAL_S)
        disk_prev = disk_cur

        elapsed = time.monotonic() - start
        if timeout_s > 0 and elapsed >= timeout_s:
            break

        time.sleep(_SAMPLE_INTERVAL_S)

    duration_s = time.monotonic() - start
    n = len(cpu_samples) or 1

    # Sectors are 512 bytes
    disk_read_written_mb = disk_total_sectors * 512 / (1024 * 1024)

    summary = {
        "label": label,
        "duration_s": round(duration_s, 1),
        "cpu": {
            "avg_usage_pct": round(sum(cpu_samples) / n, 1),
            "peak_usage_pct": round(max(cpu_samples, default=0.0), 1),
            "samples": len(cpu_samples),
        },
        "memory": {
            "avg_mb": round(sum(mem_samples) / n, 1),
            "peak_mb": round(max(mem_samples, default=0.0), 1),
            "total_mb": round(_read_mem_total_mb(), 1),
            "samples": len(mem_samples),
        },
        "disk": {
            "total_mb": round(disk_read_written_mb, 1),
            "avg_ops_per_s": round(sum(disk_ops_samples) / n, 1),
            "peak_ops_per_s": round(max(disk_ops_samples, default=0.0), 1),
            "samples": len(disk_ops_samples),
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"resource_profile: wrote {output_path} ({n} samples, {duration_s:.1f}s)", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="CI resource profiler")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--label", default="", help="Label for this profile")
    parser.add_argument("--timeout", type=float, default=0,
                        help="Max seconds to run (0 = until SIGTERM)")
    args = parser.parse_args()
    run_profiler(args.output, args.label, args.timeout)


if __name__ == "__main__":
    main()
