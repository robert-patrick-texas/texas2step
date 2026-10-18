"""Summary statistics shared by the server, the CLI and the measurement engine.

The browser client implements the same definitions in ``web/src/engine/stats.ts``;
keep the two in step.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def jitter(values: Sequence[float]) -> float:
    """Mean absolute difference between consecutive samples (RFC 3550 style, unsmoothed)."""
    if len(values) < 2:
        return 0.0
    return sum(abs(b - a) for a, b in itertools.pairwise(values)) / (len(values) - 1)


def summarize(samples: Sequence[float], lost: int = 0, keep_samples: bool = True) -> dict[str, Any]:
    """Latency-style summary in milliseconds. Samples stay in arrival order for jitter."""
    out: dict[str, Any] = {"count": len(samples), "lost": lost}
    if samples:
        mean = sum(samples) / len(samples)
        var = sum((s - mean) ** 2 for s in samples) / len(samples)
        out |= {
            "min": min(samples),
            "max": max(samples),
            "mean": mean,
            "median": percentile(samples, 50),
            "p25": percentile(samples, 25),
            "p75": percentile(samples, 75),
            "p90": percentile(samples, 90),
            "stdev": math.sqrt(var),
            "jitter": jitter(samples),
        }
    else:
        out |= dict.fromkeys(
            ("min", "max", "mean", "median", "p25", "p75", "p90", "stdev", "jitter")
        )
    if keep_samples:
        out["samples"] = [round(s, 3) for s in samples]
    return out


def warmup_seconds(duration: float) -> float:
    """Ramp-up excluded from the stable throughput figure (TCP slow start, window growth)."""
    return min(max(0.2 * duration, 1.0), 0.4 * duration)


def summarize_throughput(
    bin_bytes: Sequence[int], bin_ms: float, duration: float
) -> dict[str, Any]:
    """Turn per-bin byte counts into a timeline plus stable and peak rates (Mbps)."""
    scale = 8 / (bin_ms * 1000)  # bytes per bin -> Mbps
    timeline = [b * scale for b in bin_bytes]
    warm = warmup_seconds(duration)
    first = min(len(timeline), math.ceil(warm * 1000 / bin_ms))
    steady = timeline[first:] or timeline
    return {
        "mbps": sum(steady) / len(steady) if steady else 0.0,
        "peak_mbps": percentile(timeline, 95) if timeline else 0.0,
        "bytes": int(sum(bin_bytes)),
        "duration_s": duration,
        "warmup_s": warm,
        "bin_ms": bin_ms,
        "timeline": [round(v, 3) for v in timeline],
    }


# --- Readiness scoring ------------------------------------------------------------
# Thresholds are deliberately simple and documented in docs/methodology.md.

GRADES = ("A+", "A", "B", "C", "D", "F")
_BLOAT_LIMITS = (5, 30, 60, 200, 400)


def bufferbloat_grade(added_ms: float | None) -> str | None:
    if added_ms is None:
        return None
    for grade, limit in zip(GRADES, _BLOAT_LIMITS, strict=False):
        if added_ms < limit:
            return grade
    return "F"
