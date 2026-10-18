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
