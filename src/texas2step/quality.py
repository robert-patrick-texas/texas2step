"""What a test's figures mean for calls and for responsiveness.

Worked out from measurements a test already makes: the packet-loss test's round trips
give a call-quality score, and the latency-under-load probes give responsiveness in round
trips per minute. The browser client implements the same definitions in
``web/src/engine/quality.ts``; keep the two in step. docs/methodology.md explains them.
"""

from __future__ import annotations

import functools
import math
import operator
from collections.abc import Iterable, Sequence
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


def _total(values: Iterable[float]) -> float:
    """Added left to right, as JavaScript adds them. Python's sum() compensates for rounding
    since 3.12, so it can differ in the last bit and round the other way."""
    return functools.reduce(operator.add, values, 0.0)


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


def one_way(
    sent: Sequence[float],
    server: Sequence[float | None],
    back: Sequence[float | None],
    received: Sequence[int | None],
    total: int | None,
) -> dict[str, Any] | None:
    """Each direction's delays and losses, from the server's stamps.

    For each probe: when this client sent it and, for those echoed, when the server got it
    (by its own clock), how many it had got by then, and when the echo came back. ``total``
    is how many it got in all, None if unknown. A delay is given above the direction's
    fastest, which takes out the difference between the two clocks.

    Between two echoes, the server's counts tell how many of the probes in between reached
    it: the rest were lost on the way there (``lost_upstream``), and the others on the way
    back. Which of them went which way can't be told; those lost upstream are taken to be
    the first.
    """
    n = len(sent)
    echoed = [i for i in range(n) if server[i] is not None and back[i] is not None]
    if not echoed:
        return None
    up = {i: server[i] - sent[i] for i in echoed}  # ty: ignore[unsupported-operator]
    down = {i: back[i] - server[i] for i in echoed}  # ty: ignore[unsupported-operator]
    up_min, down_min = min(up.values()), min(down.values())
    lost_upstream: list[int] = []
    prev, had = -1, 0
    for i in [*echoed, n]:  # n: past the last probe, where the server's total stands
        count = received[i] if i < n else (had if total is None else total) + 1
        missing = i - prev - 1
        reached = min(max((count or 0) - had - 1, 0), missing)
        lost_upstream += range(prev + 1, prev + 1 + missing - reached)
        prev, had = i, max(had, count or 0)
    return {
        "up_ms": [_round(up[i] - up_min, 3) if i in up else None for i in range(n)],
        "down_ms": [_round(down[i] - down_min, 3) if i in down else None for i in range(n)],
        "lost_upstream": lost_upstream,
    }


def _judge(
    seqs: Sequence[int],
    lost: Sequence[bool],
    late: Sequence[bool],
    step: int,
    interval_ms: float,
    delay_ms: float,
) -> dict[str, Any]:
    """A call over these probes, each lost or late or neither. Its bursts count the probes
    whose sequence numbers are a call's packets: every ``step``-th."""
    impaired = [a or b for a, b in zip(lost, late, strict=True)]
    view = [bad for seq, bad in zip(seqs, impaired, strict=True) if seq % step == 0]
    pct = sum(impaired) / len(impaired) * 100
    ratio = burst_ratio(view)
    r = r_factor(delay_ms, pct, ratio or 1.0)
    score = mos(r)
    gap = longest_run(view) * interval_ms * step
    return {
        "packets": len(view),
        "late": sum(late),
        "late_pct": sum(late) / len(late) * 100,
        "impaired_pct": pct,
        "burst_ratio": None if ratio is None else _round(ratio, 2),
        "longest_gap_ms": gap,
        "r": _round(r, 1),
        "mos": _round(score, 2),
        "mos_rating": rating(score),
        "rating": heard(rating(score), gap),
    }


def call_quality(loss: dict[str, Any] | None) -> dict[str, Any] | None:
    """A call's quality over the path the packet-loss test measured, or None without one.

    A probe is impaired when lost, or when it arrived more than the jitter buffer behind
    the fastest. The shares of lost and late probes count every probe; the bursts count
    the probes a call's 20 ms packets would be, such as every other one at 10 ms.

    With the server's stamps (``up_ms``, ``down_ms`` and ``lost_upstream``, from one_way),
    each direction is a call of its own: ``down`` what you hear, ``up`` what the other end
    hears, and the figures at the top are the worse one's. Without them, a probe is late
    when its round trip was more than the buffer behind the fastest.
    """
    if not loss or loss.get("error"):
        return None
    rtts = loss.get("packets") or []
    if not any(t is not None for t in rtts):
        return None
    n = len(rtts)
    step = max(1, int(_round(CALL_MS / loss["interval_ms"])))
    delay = loss["rtt"]["median"] / 2 + CALL_DELAY_MS
    common = {"interval_ms": loss["interval_ms"] * step, "delay_ms": delay}

    def judge(seqs: Sequence[int], lost: Sequence[bool], late: Sequence[bool]) -> dict[str, Any]:
        return _judge(seqs, lost, late, step, loss["interval_ms"], delay)

    if loss.get("up_ms") is not None and loss.get("down_ms") is not None:
        upstream = set(loss.get("lost_upstream") or [])
        up_ms, down_ms = loss["up_ms"], loss["down_ms"]
        reached = [i for i in range(n) if i not in upstream]
        up = judge(
            range(n), [i in upstream for i in range(n)], [(t or 0) > BUFFER_MS for t in up_ms]
        )
        down = judge(
            reached,
            [rtts[i] is None for i in reached],
            [(down_ms[i] or 0) > BUFFER_MS for i in reached],
        )
        # The worse direction: the lower rating, then the lower score; what you hear first.
        sides = {"down": down, "up": up}
        worse = min(sides, key=lambda d: (ORDER.index(sides[d]["rating"]), sides[d]["mos"]))
        return {"basis": "each way", **common, **sides[worse], "worse": worse, **sides}
    fastest = min(t for t in rtts if t is not None)
    late = [t is not None and t - fastest > BUFFER_MS for t in rtts]
    return {"basis": "round trip", **common, **judge(range(n), [t is None for t in rtts], late)}


def trimmed_mean(values: Sequence[float]) -> float:
    """The mean of the fastest 95 %: the slowest 5 % of round trips are left out."""
    ordered = sorted(values)
    kept = ordered[: len(ordered) - math.floor(len(ordered) * TRIM)]
    return _total(kept) / len(kept)


def rpm(samples: Sequence[float] | None) -> int | None:
    """Round trips per minute from round-trip times in milliseconds."""
    if not samples:
        return None
    mean = trimmed_mean(samples)
    return int(_round(60000 / mean)) if mean > 0 else None


def _foreign_ms(c: dict[str, Any]) -> float:
    """A new connection's round trip: the mean of its steps' (TCP connect, TLS handshake and
    first request), each the mean of its fastest 95 %. Timed as one, its open time is split
    evenly among the steps it took."""
    if c.get("tcp_ms") and c.get("http_ms"):
        steps = [c["tcp_ms"], *([c["tls_ms"]] if c.get("tls_ms") else []), c["http_ms"]]
        return _total(trimmed_mean(s) for s in steps) / len(steps)
    return trimmed_mean(c["setup_ms"]) / (3 if c.get("tls") else 2)


def _side(t: dict[str, Any] | None) -> dict[str, Any] | None:
    """One direction by the draft's method: the mean of the new connections' RPM and the
    loaded connection's. None without new connections."""
    c = (t or {}).get("connections") or {}
    if not t or not c.get("setup_ms") or _foreign_ms(c) <= 0:
        return None
    foreign = int(_round(60000 / _foreign_ms(c)))
    # On HTTP/2 or 3, requests share the transfer's connection; otherwise the probes beside it.
    on_load = bool(t.get("loaded_probes"))
    samples = t["loaded_probes"] if on_load else (t.get("loaded_latency") or {}).get("samples")
    loaded_ms = trimmed_mean(samples) if samples else None
    loaded = int(_round(60000 / loaded_ms)) if loaded_ms else None
    return {
        "rpm": foreign if loaded is None else int(_round((foreign + loaded) / 2)),
        "foreign_rpm": foreign,
        "loaded_rpm": loaded,
        "setup_ms": _round(trimmed_mean(c["setup_ms"]), 3),
        "loaded_ms": None if loaded_ms is None else _round(loaded_ms, 3),
        "loaded_on": "loaded connection" if on_load else "own connection",
    }


def responsiveness(result: dict[str, Any]) -> dict[str, Any] | None:
    """Round trips per minute under load, each direction's and the lower of the two. With
    new connections timed during the transfers, by the IETF draft's method; otherwise from
    the latency-under-load probes alone."""
    sides = {d: _side(result.get(d)) for d in ("download", "upload")}
    if any(sides.values()):
        idle = (result.get("latency") or {}).get("setup_ms")
        return {
            "rpm": min(s["rpm"] for s in sides.values() if s),
            "download_rpm": sides["download"]["rpm"] if sides["download"] else None,
            "upload_rpm": sides["upload"]["rpm"] if sides["upload"] else None,
            "basis": "ippm",
            **{d: s for d, s in sides.items() if s},
            **({} if idle is None else {"idle_setup_ms": _round(idle, 3)}),
        }
    found: dict[str, Any] = {}
    for direction in ("download", "upload"):
        loaded = (result.get(direction) or {}).get("loaded_latency") or {}
        found[f"{direction}_rpm"] = rpm(loaded.get("samples"))
    known = [v for v in found.values() if v is not None]
    if not known:
        return None
    return {"rpm": min(known), **found, "basis": "latency under load"}
