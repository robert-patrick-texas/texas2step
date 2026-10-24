"""Call quality and responsiveness, worked out from a test's own figures."""

import pytest

from texas2step.quality import (
    burst_ratio,
    call_quality,
    heard,
    longest_run,
    mos,
    r_factor,
    rating,
    responsiveness,
    rpm,
    trimmed_mean,
)


def loss(packets, interval_ms=10.0, median=None):
    """A packet-loss result: one round trip per probe, None for a lost one."""
    times = sorted(t for t in packets if t is not None)
    middle = median if median is not None else (times[len(times) // 2] if times else None)
    return {"packets": packets, "interval_ms": interval_ms, "rtt": {"median": middle}}


def test_mos_follows_g107_annex_b():
    assert mos(-5) == 1.0
    assert mos(120) == 4.5
    assert mos(93.2) == pytest.approx(4.41, abs=0.01)  # every factor at its default
    assert mos(80) == pytest.approx(4.02, abs=0.01)
    assert mos(60) == pytest.approx(3.1)


@pytest.mark.parametrize(
    ("score", "name"),
    [
        (4.41, "excellent"),
        (4.25, "excellent"),
        (4.24, "good"),
        (4.0, "good"),
        (3.95, "good"),
        (3.94, "fair"),
        (3.6, "fair"),
        (3.5, "poor"),
        (3.1, "poor"),
        (3.04, "bad"),
        (1.0, "bad"),
    ],
)
def test_rating_reads_the_score_as_shown(score, name):
    # 4.25 shows as 4.3, so it is rated excellent as it reads.
    assert rating(score) == name


@pytest.mark.parametrize(
    ("score", "gap", "name"),
    [
        ("excellent", 60, "excellent"),  # concealment can still cover it
        ("excellent", 80, "good"),  # heard
        ("excellent", 180, "good"),
        ("excellent", 200, "fair"),  # a syllable gone
        ("good", 100, "good"),
        ("fair", 100, "fair"),  # never raised
        ("poor", 400, "poor"),
        ("bad", 0, "bad"),
    ],
)
def test_a_gap_a_caller_hears_caps_the_rating(score, gap, name):
    assert heard(score, gap) == name


def test_r_factor_charges_delay_and_loss():
    assert r_factor(0, 0, 1) == pytest.approx(93.2)
    # Past 177.3 ms one way, delay costs far more.
    assert r_factor(150, 0, 1) == pytest.approx(93.2 - 3.6)
    assert r_factor(277.3, 0, 1) == pytest.approx(93.2 - 0.024 * 277.3 - 11)
    # 1 % random loss with G.711's concealment, then the same loss in bursts.
    assert r_factor(0, 1, 1) == pytest.approx(93.2 - 95 / 26.1)
    assert r_factor(0, 1, 4) < r_factor(0, 1, 1)


def test_burst_ratio_and_longest_run():
    assert burst_ratio([False] * 10) is None
    spread = [i % 10 == 0 for i in range(100)]  # 10 lone losses
    assert burst_ratio(spread) == pytest.approx(0.9)
    together = [10 <= i < 20 for i in range(100)]  # one run of 10
    assert burst_ratio(together) == pytest.approx(9.0)
    assert longest_run(together) == 10
    assert longest_run(spread) == 1
    assert longest_run([]) == 0


def test_a_clean_path_is_excellent():
    call = call_quality(loss([20.0] * 500))
    assert call["basis"] == "round trip"
    # Every other probe at 10 ms is a call's 20 ms packet.
    assert (call["packets"], call["interval_ms"]) == (250, 20.0)
    assert call["late"] == 0 and call["impaired_pct"] == 0
    assert call["burst_ratio"] is None and call["longest_gap_ms"] == 0
    assert call["delay_ms"] == 10 + 80  # half the round trip, a packet and the buffer
    assert call["r"] == pytest.approx(93.2 - 0.024 * 90, abs=0.05)
    assert call["mos"] == pytest.approx(4.36, abs=0.005)
    assert call["rating"] == call["mos_rating"] == "excellent"


def test_late_probes_count_as_lost():
    packets = [20.0] * 500
    packets[100] = 20.0 + 61  # past the 60 ms buffer
    packets[200] = 20.0 + 59  # just in time
    call = call_quality(loss(packets))
    assert call["late"] == 1
    assert call["late_pct"] == pytest.approx(0.2)
    assert call["impaired_pct"] == pytest.approx(0.2)


def test_bursts_cost_more_than_the_same_loss_spread_out():
    spread = [20.0] * 500
    for i in range(0, 500, 50):  # ten lone losses, all on a call's packets
        spread[i] = None
    burst = [20.0] * 500
    for i in range(100, 110):  # the same ten in one 100 ms outage
        burst[i] = None
    a, b = call_quality(loss(spread)), call_quality(loss(burst))
    assert a["impaired_pct"] == b["impaired_pct"] == 2.0
    assert a["longest_gap_ms"] == 20 and b["longest_gap_ms"] == 100
    assert b["burst_ratio"] > a["burst_ratio"]
    assert b["mos"] < a["mos"]


def test_one_gap_rates_down_what_the_score_calls_excellent():
    packets = [20.0] * 500
    for i in range(100, 114):  # 140 ms without a word: 7 of a call's packets
        packets[i] = None
    call = call_quality(loss(packets))
    assert call["longest_gap_ms"] == 140
    # 2.8 % lost barely moves the E-model, but a caller hears the gap.
    assert call["mos"] >= 4.0
    assert call["mos_rating"] in ("excellent", "good")
    assert call["rating"] == "good"
    packets[114:124] = [None] * 10  # 240 ms: a syllable gone
    assert call_quality(loss(packets))["rating"] == "fair"


def test_the_call_view_follows_the_probe_spacing():
    assert call_quality(loss([20.0] * 100, interval_ms=20))["packets"] == 100
    assert call_quality(loss([20.0] * 100, interval_ms=5))["packets"] == 25
    assert call_quality(loss([20.0] * 100, interval_ms=50))["interval_ms"] == 50


def test_long_delay_lowers_the_score():
    near = call_quality(loss([20.0] * 100))
    far = call_quality(loss([300.0] * 100))
    assert far["delay_ms"] == 230
    assert far["mos"] < near["mos"]


def test_no_score_without_echoes():
    assert call_quality(None) is None
    assert call_quality({"error": "TimeoutError: "}) is None
    assert call_quality(loss([None] * 50)) is None


def test_trimmed_mean_leaves_out_the_slowest_5_percent():
    assert trimmed_mean([10.0] * 19 + [1000.0]) == 10.0
    assert trimmed_mean([10.0, 20.0]) == 15.0  # nothing to leave out of two


def test_rpm_and_responsiveness():
    assert rpm([10.0] * 19 + [1000.0]) == 6000
    assert rpm([]) is None
    assert rpm([0.0]) is None
    result = {
        "download": {"loaded_latency": {"samples": [20.0] * 40}},
        "upload": {"loaded_latency": {"samples": [40.0] * 40}},
    }
    assert responsiveness(result) == {
        "rpm": 1500,  # the slower direction
        "download_rpm": 3000,
        "upload_rpm": 1500,
        "basis": "latency under load",
    }
    assert responsiveness({"download": {"loaded_latency": None}}) is None
    one = responsiveness({"download": {"loaded_latency": {"samples": [30.0]}}})
    assert one == {
        "rpm": 2000,
        "download_rpm": 2000,
        "upload_rpm": None,
        "basis": "latency under load",
    }
