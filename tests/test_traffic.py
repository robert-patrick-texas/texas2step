"""Traffic limits: what the tests move, per day and month, shared through one file."""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from texas2step import cli
from texas2step.api.measure import _count, _whose
from texas2step.app import create_app
from texas2step.config import Settings
from texas2step.store import ResultStore
from texas2step.traffic import (
    GB,
    INSIDE,
    LOCAL,
    MESSAGE,
    OUTSIDE,
    TEST_SECONDS,
    TrafficMeter,
    size,
)

ZONE = ZoneInfo("America/Chicago")
NOON = datetime(2026, 10, 23, 12, tzinfo=ZONE).timestamp()


def meter(path, **kw):
    return TrafficMeter(path, zone="America/Chicago", networks=["10.0.0.0/8"], **kw)


def test_whose_traffic(tmp_path):
    m = meter(tmp_path / "t.db", day_gb=1)
    ips = ("203.0.113.9", "10.1.2.3", "127.0.0.1", "::1", "unknown")
    assert [m.kind(ip) for ip in ips] == [OUTSIDE, INSIDE, LOCAL, LOCAL, OUTSIDE]
    assert [size(n) for n in (12_100_000_000, 10 * GB, 350e6, 0)] == [
        "12.1 GB",
        "10 GB",
        "350 MB",
        "0 bytes",
    ]


@pytest.mark.parametrize(
    ("counts", "internal", "counted"),
    [("both", True, 16), ("sent", True, 12), ("received", True, 4), ("both", False, 11)],
)
def test_what_counts(tmp_path, counts, internal, counted):
    m = meter(tmp_path / "t.db", day_gb=100, counts=counts, internal=internal)
    m.add(OUTSIDE, sent=9 * GB, received=2 * GB)
    m.add(INSIDE, sent=3 * GB, received=2 * GB)
    m.add(LOCAL, sent=50 * GB, received=50 * GB)  # never on the wire
    day, month = m.periods(NOON)  # this worker's own bytes count before they are written
    assert day.counted == month.counted == counted * GB
    m.flush(NOON)
    day, _ = meter(tmp_path / "t.db", day_gb=100, counts=counts, internal=internal).periods(NOON)
    assert day.counted == counted * GB
    assert day.usage.inside_sent == 3 * GB  # kept either way, for texas2step traffic


def test_running_tests_finish_and_new_ones_wait_for_midnight(tmp_path):
    m = meter(tmp_path / "t.db", day_gb=10)
    assert m.admit("early", OUTSIDE, NOON) is None
    m.add(OUTSIDE, sent=10 * GB)
    refusal = m.admit("late", OUTSIDE, NOON + 1)
    assert refusal is not None
    assert refusal.until == datetime(2026, 10, 24, tzinfo=ZONE)
    assert refusal.reason == (
        "Today's allowance is used up; tests start again at midnight (America/Chicago)"
    )
    assert m.admit("early", OUTSIDE, NOON + 60) is None  # the test let in goes on
    assert m.admit(None, OUTSIDE, NOON + 60) is not None  # a request of no test
    assert m.admit("early", OUTSIDE, NOON + TEST_SECONDS) is not None  # but not for ever
    assert m.admit("x", INSIDE, NOON) is not None  # internal_networks count by default
    assert m.admit("x", LOCAL, NOON) is None
    m.flush(NOON + 2)
    # The other worker knows the test too.
    other = meter(tmp_path / "t.db", day_gb=10)
    assert other.admit("early", OUTSIDE, NOON + 60) is None
    assert other.admit("late", OUTSIDE, NOON + 60) is not None
    assert other.admit("tomorrow", OUTSIDE, NOON + 12 * 3600) is None


def test_internal_networks_test_without_counting(tmp_path):
    m = meter(tmp_path / "t.db", day_gb=1, internal=False)
    m.add(OUTSIDE, sent=GB)
    m.add(INSIDE, sent=5 * GB)
    assert m.admit("a", OUTSIDE, NOON) is not None
    assert m.admit("b", INSIDE, NOON) is None
    outside, inside = m.public(OUTSIDE, NOON), m.public(INSIDE, NOON)
    # Outside: the site's message, and not when tests start again, nor the figures.
    assert outside == {"open": False, "message": MESSAGE, "url": None}
    assert inside["open"] is True and inside["message"] is None
    assert inside["until"] == "2026-10-24T00:00:00-05:00"
    assert inside["reason"].startswith("Today's allowance is used up")
    assert inside["today"] == {"counted": GB, "allowance": GB}
    assert inside["month"] == {"counted": GB, "allowance": None}


def test_month_limit_and_allowing_more(tmp_path):
    m = meter(tmp_path / "t.db", day_gb=10, month_gb=12)
    m.add(OUTSIDE, sent=8 * GB)
    m.flush(datetime(2026, 10, 2, 12, tzinfo=ZONE).timestamp())
    m.add(OUTSIDE, received=4 * GB)
    m.flush(NOON)
    day, month = m.periods(NOON)
    assert (day.counted, month.counted) == (4 * GB, 12 * GB)
    refusal = m.refusal(day, month)
    assert refusal is not None
    assert refusal.until == datetime(2026, 11, 1, tzinfo=ZONE)
    assert refusal.reason == (
        "This month's allowance is used up; tests start again on November 1 (America/Chicago)"
    )
    day, month = m.allow(5, NOON)
    assert (day.allowance, month.allowance) == (15 * GB, 17 * GB)
    assert m.refusal(day, month) is None
    day, month = m.allow(-100, NOON)  # back to the limits, no lower
    assert (day.allowance, month.allowance) == (10 * GB, 12 * GB)
    assert [d for d, _ in m.days()] == ["2026-10-23", "2026-10-02"]
    # November starts again from nothing.
    november = datetime(2026, 11, 1, 0, 1, tzinfo=ZONE).timestamp()
    assert m.refusal(*m.periods(november)) is None


def test_the_journal_hears_of_80_and_100_percent_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING, "texas2step.traffic")
    a, b = meter(tmp_path / "t.db", day_gb=10), meter(tmp_path / "t.db", day_gb=10)
    a.add(OUTSIDE, sent=7 * GB)
    a.flush(NOON)
    assert not caplog.records
    b.add(OUTSIDE, sent=GB)
    b.flush(NOON)
    a.add(OUTSIDE, received=GB // 2)
    a.flush(NOON)
    assert [r.getMessage() for r in caplog.records] == [
        "traffic: 80% of today's allowance is used: 8 GB of 10 GB"
    ]
    caplog.clear()
    b.add(OUTSIDE, sent=2 * GB)
    b.flush(NOON)
    a.flush(NOON)
    assert [r.getMessage() for r in caplog.records] == [
        "traffic: today's allowance of 10 GB is used up (10.5 GB); new tests are refused "
        "until midnight (America/Chicago)"
    ]
    assert a.refusal(*a.periods(NOON)) is not None  # the other worker's bytes count


def test_api_refuses_new_tests_and_lets_running_ones_finish(tmp_path):
    settings = Settings(
        results_path=str(tmp_path / "r.db"),
        traffic_file=str(tmp_path / "t.db"),
        traffic_day_gb=10_000 / GB,
        internal_networks=["10.0.0.0/8"],
        traffic_count_internal=False,
        traffic_message="Testing is paused here.",
        traffic_url="https://houston.example.net/texas2step/",
    )
    closed = {
        "detail": "Testing is paused here. Click here to continue: "
        "https://houston.example.net/texas2step/",
        "message": "Testing is paused here.",
        "url": "https://houston.example.net/texas2step/",
    }
    with TestClient(create_app(settings)) as c:
        traffic = c.get("/api/v1/info").json()["traffic"]
        assert traffic == {"open": True, "message": None, "url": None}
        assert len(c.get("/api/v1/down?bytes=6000&test=run-1").content) == 6000
        assert c.post("/api/v1/up?test=run-1", content=b"x" * 5000).json()["bytes"] == 5000
        r = c.get("/api/v1/down?bytes=10&test=run-2")
        assert (r.status_code, r.json()) == (503, closed)
        assert "retry-after" not in r.headers  # no word of when tests start again
        r = c.post("/api/v1/up?test=run-2", content=b"x")
        assert (r.status_code, r.json()) == (503, closed)
        assert len(c.get("/api/v1/down?bytes=1000&test=run-1").content) == 1000
        traffic = c.get("/api/v1/info").json()["traffic"]
        assert traffic == {"open": False, "message": closed["message"], "url": closed["url"]}
        assert c.get("/api/v1/down?bytes=1&test=not%20an%20id").status_code == 422
    # The service wrote its bytes as it stopped; the desk tests without counting.
    with TestClient(create_app(settings), client=("10.1.2.3", 50000)) as desk:
        traffic = desk.get("/api/v1/info").json()["traffic"]
        assert traffic["open"] is True
        assert traffic["today"] == {"counted": 12_000, "allowance": 10_000}
        assert desk.get("/api/v1/down?bytes=100&test=desk").status_code == 200


def test_without_a_limit_nothing_is_counted(tmp_path):
    settings = Settings(results_path=str(tmp_path / "r.db"), traffic_file=str(tmp_path / "t.db"))
    with TestClient(create_app(settings)) as c:
        assert "traffic" not in c.get("/api/v1/info").json()
        assert c.get("/api/v1/down?bytes=10&test=a").status_code == 200
    assert not (tmp_path / "t.db").exists()


def test_server_to_server_tests_count_this_servers_side(tmp_path):
    m = meter(tmp_path / "t.db", day_gb=1)
    _count(m, OUTSIDE, {"direction": "download", "bytes": 300})
    full = {
        "latency": {"median": 9.0},
        "download": {"direction": "download", "bytes": 100},
        "upload": {"direction": "upload", "bytes": 20},
    }
    _count(m, OUTSIDE, full)
    day, _ = m.periods(NOON)
    assert (day.usage.sent, day.usage.received) == (20, 400)
    targets = ("http://127.0.0.1:9", "https://10.2.3.4", "https://203.0.113.5/speedtest")
    assert [asyncio.run(_whose(t, m)) for t in targets] == [LOCAL, INSIDE, OUTSIDE]


def test_history_shows_each_tests_traffic(tmp_path):
    store = ResultStore(tmp_path / "r.db")
    both = store.add({"download": {"bytes": 1_250_000_000}, "upload": {"bytes": 6e8}}, None)
    neither = store.add({"latency": {"median": 9.0}}, None)
    odd = store.add({"download": {"bytes": "lots"}, "upload": {"bytes": 5}}, None)
    assert (both["traffic_bytes"], neither["traffic_bytes"], odd["traffic_bytes"]) == (
        1_850_000_000,
        None,
        5,
    )
    listed = {r["id"]: r["traffic_bytes"] for r in store.recent()}
    assert listed == {both["id"]: 1_850_000_000, neither["id"]: None, odd["id"]: 5}


def test_traffic_settings_are_checked():
    with pytest.raises(ValueError, match="traffic_counts"):
        Settings(traffic_counts="egress")
    with pytest.raises(ValueError, match="traffic_day_gb"):
        Settings(traffic_day_gb=-1)
    with pytest.raises(ValueError, match="traffic_message"):
        Settings(traffic_message=" ")
    with pytest.raises(ValueError, match="traffic_url"):
        Settings(traffic_url="javascript:alert(1)")
    assert Settings().traffic_message == MESSAGE == "Testing temporarily disabled."
    assert not Settings().traffic_limited
    assert Settings(traffic_month_gb=500).traffic_limited


def test_traffic_command(tmp_path, capsys):
    db = tmp_path / "t.db"
    config = tmp_path / "config.toml"
    config.write_text(
        f'traffic_day_gb = 10\ntraffic_file = "{db}"\naccess_timezone = "America/Chicago"\n'
        'traffic_url = "https://desk.example.net/speedtest"\n'
    )
    m = TrafficMeter(db, day_gb=10, zone="America/Chicago")
    m.add(OUTSIDE, sent=10 * GB)
    m.flush()
    m.close()
    cli.main(["traffic", "--config", str(config)])
    out = capsys.readouterr().out
    assert "Traffic limit: 10 GB a day, no monthly limit." in out
    assert "New tests are refused: Today's allowance is used up; tests start again at" in out
    link = "Click here to continue. (links to https://desk.example.net/speedtest)"
    assert f"Visitors read: {MESSAGE} {link}\n" in out
    cli.main(["traffic", "allow", "5", "--config", str(config)])
    out = capsys.readouterr().out
    assert "Allowed 5 GB more today, and so this month." in out
    assert out.rstrip().endswith("Tests are open.")

    config.write_text(f'traffic_file = "{tmp_path / "none.db"}"\n')
    cli.main(["traffic", "--config", str(config)])
    assert capsys.readouterr().out.startswith("No traffic limit is set")
    assert not (tmp_path / "none.db").exists()
    with pytest.raises(SystemExit, match="no traffic limit is set"):
        cli.main(["traffic", "allow", "5", "--config", str(config)])
