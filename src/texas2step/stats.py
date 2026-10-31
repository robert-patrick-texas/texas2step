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
        "rate_limit": rate_limit(timeline, bin_bytes, bin_ms, first),
    }


# A rate limit: a rate that holds, then steps down and stays down to the end, as a shaper,
# a policer or the end of an ISP's speed boost makes it.
LIMIT_HIGH_S = 1.0  # the higher rate lasts at least this long after ramp-up
LIMIT_LOW_S = 2.0  # and the lower rate at least this long, to the end
LIMIT_SHARE = 0.75  # the lower rate is at most this share of the higher


def rate_limit(
    timeline: Sequence[float], bin_bytes: Sequence[int], bin_ms: float, first: int
) -> dict[str, Any] | None:
    """Where a transfer's rate stepped down and stayed down; None when it didn't.

    The bins after ramp-up (from ``first``) are split in two where a higher level then a
    lower one fit them best, anywhere. It is a rate limit when the higher part lasts at
    least LIMIT_HIGH_S and the lower LIMIT_LOW_S; the lower level is above zero and at most
    LIMIT_SHARE of the higher; the lower part's 90th percentile is below the higher part's
    median, so a wobbling rate isn't one; and the last LIMIT_HIGH_S is at most LIMIT_SHARE
    of the higher level too, so a dip that recovered isn't one.
    """
    xs = timeline[first:]
    n = len(xs)
    high = math.ceil(LIMIT_HIGH_S * 1000 / bin_ms)
    low = math.ceil(LIMIT_LOW_S * 1000 / bin_ms)
    if n < high + low:
        return None
    # Running sums, added left to right as stats.ts and t2s add them, so all three split
    # in the same place.
    heads = list(itertools.accumulate(xs))
    total = heads[-1]
    # The best split leaves the two parts' means furthest apart for their sizes: the
    # largest S1²/k + S2²/(n-k), with S1 the first k bins' sum and S2 the rest's. It is
    # sought everywhere, then held to the two lengths: sought only where they allow, a step
    # too late to tell would be split early, mixing the higher rate into the lower.
    best, split = 0.0, 0
    for k in range(1, n):
        head, rest = heads[k - 1], total - heads[k - 1]
        if head / k <= rest / (n - k):
            continue  # not a step down
        fit = head * head / k + rest * rest / (n - k)
        if fit > best:
            best, split = fit, k
    if split < high or n - split < low:
        return None
    from_mbps = heads[split - 1] / split
    to_mbps = (total - heads[split - 1]) / (n - split)
    last_mbps = (total - heads[n - high - 1]) / high
    if not (
        0 < to_mbps <= LIMIT_SHARE * from_mbps
        and percentile(xs[split:], 90) < percentile(xs[:split], 50)
        and last_mbps <= LIMIT_SHARE * from_mbps
    ):
        return None
    at = first + split
    return {
        "from_mbps": from_mbps,
        "to_mbps": to_mbps,
        "at_s": at * bin_ms / 1000,
        "before_bytes": int(sum(bin_bytes[:at])),
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
