import math

import pytest

from texas2step.stats import (
    bufferbloat_grade,
    jitter,
    percentile,
    summarize,
    summarize_throughput,
    warmup_seconds,
)


def test_percentile_interpolates():
    values = [1, 2, 3, 4]
    assert percentile(values, 0) == 1
    assert percentile(values, 100) == 4
    assert percentile(values, 50) == 2.5
    assert percentile([5], 90) == 5


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_jitter_is_mean_consecutive_difference():
    assert jitter([10, 12, 11, 15]) == pytest.approx((2 + 1 + 4) / 3)
    assert jitter([7]) == 0.0


def test_summarize_keeps_order_and_counts_loss():
    s = summarize([3.0, 1.0, 2.0], lost=2)
    assert s["count"] == 3
    assert s["lost"] == 2
    assert s["median"] == 2.0
    assert s["min"] == 1.0 and s["max"] == 3.0
    assert s["jitter"] == pytest.approx(1.5)
    assert s["samples"] == [3.0, 1.0, 2.0]
    assert s["stdev"] == pytest.approx(math.sqrt(2 / 3))


def test_summarize_empty_has_nulls():
    s = summarize([], lost=5, keep_samples=False)
    assert s["median"] is None and s["lost"] == 5 and "samples" not in s


def test_warmup_bounds():
    assert warmup_seconds(10) == 2.0
    assert warmup_seconds(3) == 1.0
    assert warmup_seconds(2) == pytest.approx(0.8)  # never more than 40 %


def test_throughput_excludes_warmup():
    # 10 s at 100 ms bins: slow first 2 s, then a steady 125,000 B/bin = 10 Mbps.
    bins = [1_000] * 20 + [125_000] * 80
    t = summarize_throughput(bins, 100, 10)
    assert t["mbps"] == pytest.approx(10.0)
    assert t["bytes"] == sum(bins)
    assert len(t["timeline"]) == 100
    assert t["peak_mbps"] == pytest.approx(10.0)
    assert t["rate_limit"] is None


MBPS = 12_500  # bytes in a 100 ms bin per Mb/s


def _steps(*levels: tuple[int, float]) -> list[int]:
    """Bins for (count, Mb/s) levels, each with a fixed ±8 % wobble."""
    wobble = (1.0, 1.08, 0.94, 1.03, 0.92, 1.06, 0.97)
    return [
        int(mbps * wobble[i % len(wobble)] * MBPS) for count, mbps in levels for i in range(count)
    ]


def test_rate_limit_finds_a_step_down():
    # Ramp-up is the first 2 s; the rate holds at 940 to 4.2 s, then 310 to the end.
    bins = _steps((42, 940), (58, 310))
    limit = summarize_throughput(bins, 100, 10)["rate_limit"]
    assert limit["at_s"] == pytest.approx(4.2)
    assert limit["from_mbps"] == pytest.approx(940, rel=0.03)
    assert limit["to_mbps"] == pytest.approx(310, rel=0.03)
    assert limit["before_bytes"] == sum(bins[:42])


def test_rate_limit_finds_a_boost_that_ends():
    limit = summarize_throughput(_steps((120, 940), (80, 500)), 100, 20)["rate_limit"]
    assert limit["at_s"] == pytest.approx(12.0)


@pytest.mark.parametrize(
    ("levels", "why"),
    [
        (((100, 650),), "one level"),
        (((42, 940), (58, 800)), "a drop of only 15 %"),
        (((40, 940), (30, 300), (30, 940)), "a dip that recovers"),
        (((85, 940), (15, 300)), "the lower rate lasts under 2 s"),
        (((25, 940), (75, 300)), "the higher rate lasts under 1 s after ramp-up"),
        (((40, 300), (60, 940)), "a step up"),
        (((42, 940), (58, 0)), "a stall, not a limit"),
    ],
)
def test_rate_limit_ignores(levels, why):
    assert summarize_throughput(_steps(*levels), 100, 10)["rate_limit"] is None, why


def test_rate_limit_ignores_a_wobbling_rate():
    # Wi-Fi: lower and higher rates in turn, never settling on either.
    bins = _steps(*[(5, 900.0), (5, 450.0)] * 10)
    assert summarize_throughput(bins, 100, 10)["rate_limit"] is None


@pytest.mark.parametrize(
    ("added", "grade"),
    [
        (None, None),
        (0, "A+"),
        (4.9, "A+"),
        (29, "A"),
        (59, "B"),
        (150, "C"),
        (399, "D"),
        (900, "F"),
    ],
)
def test_bufferbloat_grade(added, grade):
    assert bufferbloat_grade(added) == grade
