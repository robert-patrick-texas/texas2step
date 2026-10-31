"""Write tests/methodology-cases.json: inputs and what texas2step makes of them.

The Python engine is the reference. t2s, the Go client, checks its own statistics, loss
summary, one-way delays, call quality and responsiveness against these cases
(t2s/internal/*_test.go), and tests/test_methodology_cases.py checks that the file still
says what the Python code does. After changing the methodology, run this again:

    uv run python scripts/methodology_cases.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from texas2step import quality, stats
from texas2step.engine import bufferbloat, loss_summary

OUT = Path(__file__).resolve().parent.parent / "tests" / "methodology-cases.json"


def _rtts(rng: random.Random, n: int, base: float, spread: float) -> list[float]:
    return [
        round(base + rng.random() * spread + (rng.random() < 0.05) * spread * 8, 6)
        for _ in range(n)
    ]


def _path(rng: random.Random, n: int, interval: float, up_loss: float, down_loss: float) -> dict:
    """A simulated packet-loss test: what the client and the server's stamps saw."""
    offset = 12345.678  # the server's clock against the client's
    sent = [i * interval + rng.random() * 0.2 for i in range(n)]
    arrive: dict[int, float] = {}
    burst = 0
    for i in range(n):
        if burst or rng.random() < up_loss:
            burst = burst - 1 if burst else rng.choice((0, 0, 1, 3))
            continue
        arrive[i] = sent[i] + 8 + rng.random() * 6 + (rng.random() < 0.03) * 90
    by_arrival = sorted(arrive, key=lambda i: arrive[i])
    count = {i: k + 1 for k, i in enumerate(by_arrival)}
    back: dict[int, float] = {}
    for i in arrive:
        if rng.random() >= down_loss:
            back[i] = arrive[i] + 7 + rng.random() * 5 + (rng.random() < 0.03) * 70
    order = sorted(back, key=lambda i: back[i])
    rtts = [round(back[i] - sent[i], 6) if i in back else None for i in range(n)]
    return {
        "rtts": rtts,
        "order": order,
        "duplicates": rng.choice((0, 0, 1)),
        "server": {"rx": len(arrive), "tx": len(arrive)},
        "sent": [round(t, 6) for t in sent],
        "stamp_at": [round(arrive[i] + offset, 6) if i in back else None for i in range(n)],
        "back": [round(back[i], 6) if i in back else None for i in range(n)],
        "counts": [count[i] if i in back else None for i in range(n)],
    }


def _connections(rng: random.Random, n: int, tls: bool) -> dict:
    tcp = _rtts(rng, n, 10, 20)
    http = _rtts(rng, n, 10, 25)
    out: dict[str, Any] = {"tcp_ms": tcp, "http_ms": http, "lost": rng.choice((0, 1)), "tls": tls}
    if tls:
        out["tls_ms"] = _rtts(rng, n, 12, 22)
    steps = [tcp, http, *([out["tls_ms"]] if tls else [])]
    out["setup_ms"] = [round(sum(s), 3) for s in zip(*steps, strict=True)]
    return out


def cases() -> dict[str, list[dict]]:
    rng = random.Random(20261027)
    out: dict[str, list[dict]] = {
        key: []
        for key in (
            "percentile",
            "jitter",
            "summarize",
            "summarize_throughput",
            "bufferbloat",
            "loss",
            "call_quality",
            "responsiveness",
            "rpm",
        )
    }

    for n in (1, 2, 5, 37, 200):
        values = _rtts(rng, n, 20, 15)
        for q in (0, 25, 50, 90, 95, 100):
            out["percentile"].append(
                {"values": values, "q": q, "want": stats.percentile(values, q)}
            )
        out["jitter"].append({"values": values, "want": stats.jitter(values)})
    out["jitter"].append({"values": [], "want": stats.jitter([])})

    for samples, lost, keep in (
        ([], 0, True),
        ([], 3, False),
        ([12.5], 0, True),
        (_rtts(rng, 20, 18, 9), 2, True),
        (_rtts(rng, 500, 30, 4), 0, False),
    ):
        out["summarize"].append(
            {
                "samples": samples,
                "lost": lost,
                "keep_samples": keep,
                "want": stats.summarize(samples, lost, keep),
            }
        )

    for seconds, bins in ((10.0, 100), (2.5, 25), (1.0, 3), (10.0, 0)):
        series = [int(rng.random() * 1.2e6 * min(1, (i + 1) / 12)) for i in range(bins)]
        out["summarize_throughput"].append(
            {
                "bins": series,
                "bin_ms": 100,
                "seconds": seconds,
                "want": stats.summarize_throughput(series, 100, seconds),
            }
        )
    # Rates that step down partway, and some that only seem to: the rate limit's cases.
    for seconds, levels in (
        (10.0, ((30, 940.0), (70, 310.0))),  # holds for 3 s, then 310 to the end
        (10.0, ((30, 940.0), (30, 300.0), (40, 940.0))),  # a dip that recovers
        (20.0, ((120, 940.0), (80, 500.0))),  # a speed boost that ends at 12 s
        (10.0, ((85, 940.0), (15, 300.0))),  # a drop too late to tell
        (10.0, ((100, 650.0),)),  # one level
    ):
        series = [
            int(mbps * (1 + rng.gauss(0, 0.08)) * 12_500)  # Mb/s to bytes in 100 ms
            for count, mbps in levels
            for _ in range(count)
        ]
        out["summarize_throughput"].append(
            {
                "bins": series,
                "bin_ms": 100,
                "seconds": seconds,
                "want": stats.summarize_throughput(series, 100, seconds),
            }
        )

    for idle, down, up in (
        (20.0, 22.0, 80.0),
        (20.0, None, 450.0),
        (None, 30.0, 30.0),
        (15.0, 18.0, None),
        (10.0, 14.0, 15.0),
    ):
        result = {
            "latency": {"median": idle},
            "download": {"loaded_latency": None if down is None else {"median": down}},
            "upload": {"loaded_latency": None if up is None else {"median": up}},
        }
        out["bufferbloat"].append({"result": result, "want": bufferbloat(result)})

    for n, interval, up_loss, down_loss, stamped in (
        (500, 10.0, 0.0, 0.0, True),
        (500, 10.0, 0.01, 0.01, True),
        (300, 20.0, 0.05, 0.0, True),
        (200, 10.0, 0.0, 0.08, False),
        (100, 30.0, 0.02, 0.02, False),
        (50, 10.0, 1.0, 0.0, True),
    ):
        p = _path(rng, n, interval, up_loss, down_loss)
        summary = loss_summary(
            p["rtts"], p["order"], p["duplicates"], p["server"], "webrtc", interval, 200
        )
        ways = (
            quality.one_way(p["sent"], p["stamp_at"], p["back"], p["counts"], p["server"]["rx"])
            if stamped
            else None
        )
        loss = summary | (ways or {})
        out["loss"].append(
            {
                **p,
                "interval_ms": interval,
                "size": 200,
                "stamped": stamped,
                "want": summary,
                "want_one_way": ways,
            }
        )
        out["call_quality"].append({"loss": loss, "want": quality.call_quality(loss)})
    out["call_quality"].append({"loss": {"error": "TimeoutError: no answer"}, "want": None})

    for tls, loaded_probes in ((True, False), (False, False), (True, True)):
        result: dict[str, Any] = {"latency": {"setup_ms": 33.25}}
        for direction in ("download", "upload"):
            t: dict[str, Any] = {
                "connections": _connections(rng, 9, tls),
                "loaded_latency": {"samples": _rtts(rng, 40, 40, 60)},
            }
            if loaded_probes:
                t["loaded_probes"] = _rtts(rng, 38, 35, 50)
            result[direction] = t
        out["responsiveness"].append({"result": result, "want": quality.responsiveness(result)})
    old = {
        "connections": {"setup_ms": _rtts(rng, 6, 40, 30), "lost": 0, "tls": True},
        "loaded_latency": {"samples": _rtts(rng, 30, 40, 60)},
    }
    only = {"download": old, "upload": None}
    out["responsiveness"].append({"result": only, "want": quality.responsiveness(only)})
    fallback = {
        d: {"loaded_latency": {"samples": _rtts(rng, 30, 50, 40)}} for d in ("download", "upload")
    }
    out["responsiveness"].append({"result": fallback, "want": quality.responsiveness(fallback)})
    out["responsiveness"].append({"result": {}, "want": quality.responsiveness({})})

    for samples in (_rtts(rng, 1, 30, 1), _rtts(rng, 19, 30, 20), _rtts(rng, 200, 25, 10), []):
        out["rpm"].append({"samples": samples, "want": quality.rpm(samples)})
    return out


def render() -> str:
    """One case to a line, so a change to the methodology shows as the cases it changes."""
    groups = [
        f"{json.dumps(key)}: [\n" + ",\n".join(json.dumps(case) for case in found) + "\n]"
        for key, found in cases().items()
    ]
    return "{\n" + ",\n".join(groups) + "\n}\n"


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT}")
