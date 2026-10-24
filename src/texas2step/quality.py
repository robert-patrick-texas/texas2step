"""What a test's figures mean for calls and for responsiveness.

Worked out from measurements a test already makes: the packet-loss test's round trips
give a call-quality score, and the latency-under-load probes give responsiveness in round
trips per minute. The browser client implements the same definitions in
``web/src/engine/quality.ts``; keep the two in step. docs/methodology.md explains them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

CALL_MS = 20  # a call's packet interval
BUFFER_MS = 60  # a call's jitter buffer: a packet further behind the fastest is too late
# What a call adds to the network's one-way delay: filling a packet, then the buffer.
CALL_DELAY_MS = CALL_MS + BUFFER_MS
# The E-model (ITU-T G.107) for G.711 with packet-loss concealment: Ie 0 and Bpl 25.1
# (ITU-T G.113 Appendix I), and R 93.2 with every other factor at its default.
BPL = 25.1
R_DEFAULT = 93.2
# Lowest MOS of each rating, read to one decimal as it is shown.
RATINGS = ((4.3, "excellent"), (4.0, "good"), (3.6, "fair"), (3.1, "poor"))
ORDER = ("bad", "poor", "fair", "good", "excellent")
# The E-model barely charges a gap at low loss, yet a caller hears one. G.711's concealment
# fades to silence 60 ms into a gap, and a syllable lasts about 200 ms.
CONCEALED_MS = 60  # a longer gap is heard: at best "good"
SYLLABLE_MS = 200  # a gap this long takes a syllable: at best "fair"
TRIM = 0.05  # responsiveness leaves out the slowest 5 % of round trips


def _round(value: float, digits: int = 0) -> float:
    """Half up, as JavaScript's Math.round does (Python's round() rounds half to even)."""
    scale = 10**digits
    return math.floor(value * scale + 0.5) / scale


def mos(r: float) -> float:
    """Mean opinion score, 1 to 4.5, from the E-model's rating R (ITU-T G.107 Annex B)."""
    if r <= 0:
        return 1.0
    if r >= 100:
        return 4.5
    return 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)


def rating(score: float) -> str:
    shown = _round(score, 1)
    return next((name for floor, name in RATINGS if shown >= floor), "bad")


def heard(score_rating: str, longest_gap_ms: float) -> str:
    """The rating as a caller would hear it: the E-model's, at most what the longest gap
    allows."""
    if longest_gap_ms >= SYLLABLE_MS:
        best = "fair"
    elif longest_gap_ms > CONCEALED_MS:
        best = "good"
    else:
        return score_rating
    return min(score_rating, best, key=ORDER.index)


def r_factor(delay_ms: float, impaired_pct: float, burst_ratio: float) -> float:
    """The E-model's R for a call with this mouth-to-ear delay, with this share of its
    packets lost or too late, in bursts this much longer than random loss would give."""
    # The delay impairment, as Cole and Rosenbluth fitted it to G.107's.
    delay = 0.024 * delay_ms + 0.11 * max(0.0, delay_ms - 177.3)
    loss = 95 * impaired_pct / (impaired_pct / burst_ratio + BPL)
    return R_DEFAULT - delay - loss


def burst_ratio(impaired: Sequence[bool]) -> float | None:
    """How much longer the runs of impaired packets are than random loss would make them
    (the ITU's BurstR): 1 for random loss, more for bursts. None without any."""
    count = sum(impaired)
    if not count:
        return None
    runs = sum(1 for i, bad in enumerate(impaired) if bad and (i == 0 or not impaired[i - 1]))
    # Random loss with probability p gives runs 1 / (1 - p) long on average.
    return count / runs * (1 - count / len(impaired))


def longest_run(impaired: Sequence[bool]) -> int:
    longest = run = 0
    for bad in impaired:
        run = run + 1 if bad else 0
        longest = max(longest, run)
    return longest


def call_quality(loss: dict[str, Any] | None) -> dict[str, Any] | None:
    """A call's quality over the path the packet-loss test measured, or None without one.

    A probe is impaired when lost, or when it came back more than the jitter buffer
    behind the fastest. The shares of lost and late probes count every probe; the bursts
    count the probes a call's 20 ms packets would be, such as every other one at 10 ms.
    """
    if not loss or loss.get("error"):
        return None
    rtts = loss.get("packets") or []
    times = [t for t in rtts if t is not None]
    if not times:
        return None
    fastest = min(times)
    late = [t is not None and t - fastest > BUFFER_MS for t in rtts]
    impaired = [t is None or bad for t, bad in zip(rtts, late, strict=True)]
    step = max(1, int(_round(CALL_MS / loss["interval_ms"])))
    view = impaired[::step]
    pct = sum(impaired) / len(impaired) * 100
    ratio = burst_ratio(view)
    delay = loss["rtt"]["median"] / 2 + CALL_DELAY_MS
    r = r_factor(delay, pct, ratio or 1.0)
    score = mos(r)
    gap = longest_run(view) * loss["interval_ms"] * step
    return {
        "basis": "round trip",
        "packets": len(view),
        "interval_ms": loss["interval_ms"] * step,
        "late": sum(late),
        "late_pct": sum(late) / len(rtts) * 100,
        "impaired_pct": pct,
        "burst_ratio": None if ratio is None else _round(ratio, 2),
        "longest_gap_ms": gap,
        "delay_ms": delay,
        "r": _round(r, 1),
        "mos": _round(score, 2),
        "mos_rating": rating(score),
        "rating": heard(rating(score), gap),
    }


def trimmed_mean(values: Sequence[float]) -> float:
    """The mean of the fastest 95 %: the slowest 5 % of round trips are left out."""
    ordered = sorted(values)
    kept = ordered[: len(ordered) - math.floor(len(ordered) * TRIM)]
    return sum(kept) / len(kept)


def rpm(samples: Sequence[float] | None) -> int | None:
    """Round trips per minute from round-trip times in milliseconds."""
    if not samples:
        return None
    mean = trimmed_mean(samples)
    return int(_round(60000 / mean)) if mean > 0 else None


def responsiveness(result: dict[str, Any]) -> dict[str, Any] | None:
    """Round trips per minute under load, each direction's and the lower of the two."""
    found: dict[str, Any] = {}
    for direction in ("download", "upload"):
        loaded = (result.get(direction) or {}).get("loaded_latency") or {}
        found[f"{direction}_rpm"] = rpm(loaded.get("samples"))
    known = [v for v in found.values() if v is not None]
    if not known:
        return None
    return {"rpm": min(known), **found, "basis": "latency under load"}
