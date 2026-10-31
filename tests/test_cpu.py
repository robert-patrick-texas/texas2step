"""The server's CPU (cpu.py): the figures from the workers' rows, the minutes they add up
to, keeping tests out while other work is busy, the journal's warnings, and the API."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from texas2step import cli, cpu
from texas2step.app import create_app
from texas2step.config import Settings
from texas2step.cpu import COLUMNS, Busy, CpuMonitor, Nginx, Watched
from texas2step.rtc import RtcEcho, Session

INSIDE = {"X-Forwarded-For": "10.1.2.3"}
NOON = 1_793_448_000.0  # 2026-10-31 12:00 UTC


def write(
    m: CpuMonitor,
    worker: int,
    at: float,
    cpu_s: float,
    *,
    sent: int = 0,
    received: int = 0,
    probes: int = 0,
    sessions: int = 0,
    busy: float | None = None,
    total: float | None = None,
    nginx: float | None = None,
    cores: int | None = 4,
    memory: int = 100_000_000,
) -> None:
    row = (worker, at, cpu_s, memory, sent, received, probes, sessions, busy, total, nginx, cores)
    with m._lock:
        m._db.execute(f"INSERT INTO samples ({COLUMNS}) VALUES ({', '.join('?' * 12)})", row)


def host(m: CpuMonitor, t0: float, seconds: float, *, share: float, ours: float) -> None:
    """Two workers' rows every 5 s for ``seconds``: together ``ours`` cores, and the host
    busy at ``share`` of its 4 cores."""
    for i in range(int(seconds // 5) + 1):
        at = t0 + i * 5
        busy, total = share * 4 * i * 5, 4 * i * 5
        write(m, 1, at, ours / 2 * i * 5, busy=busy, total=total, nginx=0.0)
        write(m, 2, at, ours / 2 * i * 5, busy=busy, total=total, nginx=0.0)


@pytest.fixture
def monitor() -> CpuMonitor:
    return CpuMonitor(":memory:")


def test_figures_over_a_span(monitor):
    for at in (0, 30):
        k = at / 30
        write(
            monitor,
            1,
            at,
            6 * k,
            sent=int(30e6 * k),
            probes=int(300 * k),
            sessions=2,
            busy=60 * k,
            total=120 * k,
            nginx=1.5 * k,
        )
        write(monitor, 2, at, 3 * k, received=int(15e6 * k))
    got = monitor.now(30, now=30)
    assert got is not None
    one, two = got.workers
    assert (one.pid, one.cpu, one.sent, one.probes, one.sessions) == (1, 0.2, 1e6, 10, 2)
    assert (two.pid, two.cpu, two.received) == (2, 0.1, 0.5e6)
    assert got.texas2step == pytest.approx(0.3)
    assert got.nginx == pytest.approx(0.05)
    assert got.host == 0.5
    # Half the host's 4 cores are busy, and texas2step and nginx used 0.35 of them.
    assert got.other == pytest.approx(0.5 - 0.35 / 4)
    public = got.public()
    assert public["sent_mbps"] == 8 and public["received_mbps"] == 4
    assert public["probes_per_s"] == 10 and public["sessions"] == 2
    # Workers that stopped writing are left out of "now".
    assert monitor.now(30, now=60) is None


def test_the_minutes_add_up(monitor):
    """Each minute starts at the row its last one ended at, so a worker that starts
    mid-minute isn't stretched over the whole of it."""
    start = 1_000 * 60 + 30  # half a minute in
    for i in range(43):  # to 3 minutes and 30 s later
        at = start + i * 5
        write(monitor, 7, at, 0.5 * i, sent=5_000_000 * i, sessions=1)
    end = start + 42 * 5
    assert monitor._roll(end + 60)
    rows = monitor.minutes(0)
    assert [r["minute"] for r in rows] == [60_000, 60_060, 60_120, 60_180]
    first = rows[0]
    # 30 s of the first minute at 1 MB/s: 30 MB, an average of 0.5 MB/s over the minute.
    assert first["sent"] == pytest.approx(0.5e6)
    assert all(r["sent"] == pytest.approx(1e6) for r in rows[1:3])
    assert rows[1]["texas2step"] == pytest.approx(0.1)
    total = sum(r["sent"] * 60 for r in rows)
    last_by_end = max(at for at in (start + i * 5 for i in range(43)) if at <= 60_240)
    assert total == pytest.approx(5_000_000 * (last_by_end - start) / 5)
    # Rolled once: the other worker's roll adds nothing.
    other = CpuMonitor(":memory:")
    other._conn, other._lock = monitor._conn, monitor._lock
    other._roll(end + 60)
    assert len(monitor.minutes(0)) == 4


def test_other_work_keeps_tests_out(monitor, caplog):
    monitor.busy_share, monitor.busy_seconds = 0.5, 30
    t0 = 10_000.0
    # texas2step's own load, with the host nearly full, isn't other work.
    host(monitor, t0, 30, share=0.9, ours=3.6)
    monitor._judge(t0 + 30)
    assert monitor.busy is None
    assert monitor.admit("early", t0 + 30)  # let in, and remembered
    with monitor._lock:
        monitor._db.execute("DELETE FROM samples")
    # Other work at 80%: new tests are refused, the one let in before goes on.
    host(monitor, t0 + 40, 30, share=0.85, ours=0.2)
    with caplog.at_level(logging.WARNING, "texas2step.cpu"):
        monitor._judge(t0 + 70)
        monitor._judge(t0 + 70)  # told once
    assert monitor.refusing()
    told = [r.getMessage() for r in caplog.records]
    assert told == [
        "cpu: other work is using 80% of the host's CPU (cpu_busy_percent = 50): new "
        "download and upload tests and Video calls are refused"
    ]
    assert monitor.admit("early", t0 + 70)
    assert not monitor.admit("late", t0 + 70)
    assert not monitor.admit(None, t0 + 70)
    # The other worker learns of "early" from the file.
    other = CpuMonitor(":memory:", busy_percent=50)
    other._conn, other._lock = monitor._conn, monitor._lock
    other.busy = Busy(t0, 0.8)
    assert other.admit("early", t0 + 70) and not other.admit("late", t0 + 70)
    # The command line reads the state the journal was told.
    assert monitor.noted_busy(50, 30)["refusing"] is True
    # Just under the setting isn't enough: under nine tenths of it is.
    with monitor._lock:
        monitor._db.execute("DELETE FROM samples")
    host(monitor, t0 + 80, 30, share=0.52, ours=0.2)  # other work at 47%
    monitor._judge(t0 + 110)
    assert monitor.refusing()
    # Other work eases off: tests are let in again.
    with monitor._lock:
        monitor._db.execute("DELETE FROM samples")
    host(monitor, t0 + 100, 30, share=0.2, ours=0.2)
    caplog.clear()
    with caplog.at_level(logging.WARNING, "texas2step.cpu"):
        monitor._judge(t0 + 130)
    assert not monitor.refusing() and monitor.admit("late", t0 + 130)
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith("cpu: other work is down to 15% of the host's CPU: tests are let in")
    assert monitor.noted_busy(50, 30)["refusing"] is False
    assert monitor.noted_busy(0, 30) is None


def test_without_a_setting_nothing_is_kept_out(monitor):
    monitor.busy = Busy(0, 1.0)
    assert monitor.admit(None) and monitor.busy_state() is None


def minute(m: CpuMonitor, at: float, used: float, **kw: Any) -> None:
    row = {"sent": 0, "received": 0, "probes": 0, "sessions": 0} | kw
    with m._lock:
        m._db.execute(
            "INSERT INTO minutes VALUES (?, ?, ?, NULL, NULL, NULL, 4, ?, ?, ?, ?, 0, 2)",
            (at, used, used, row["sent"], row["received"], row["probes"], row["sessions"]),
        )


def test_cpu_with_nothing_to_serve_warns(monitor, caplog):
    for i in range(10):
        minute(monitor, NOON - 600 + i * 60, 0.4)
    with caplog.at_level(logging.WARNING, "texas2step.cpu"):
        monitor._watch(NOON + 20)
        monitor._watch(NOON + 80)  # told once an hour
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith("cpu: the workers used 40% of a core for the last 10 min with no tests")
    # Serving probes, the same CPU is what they cost.
    busy = CpuMonitor(":memory:")
    for i in range(10):
        minute(busy, NOON - 600 + i * 60, 0.4, probes=800.0, sessions=5)
    caplog.clear()
    with caplog.at_level(logging.WARNING, "texas2step.cpu"):
        busy._watch(NOON + 20)
    assert caplog.records == []


def test_idle_cpu_climbing_warns(monitor, caplog):
    for day in range(8, 0, -1):
        for i in range(90):
            minute(monitor, NOON - day * 86400 + i * 60, 0.01)
    for i in range(90):
        minute(monitor, NOON - 6 * 3600 + i * 60, 0.05)
    days = monitor.days(NOON)
    assert [d["idle"] for d in days[:2]] == [0.05, 0.01]
    assert days[0]["day"] == "2026-10-31" and days[0]["quiet_minutes"] == 90
    with caplog.at_level(logging.WARNING, "texas2step.cpu"):
        monitor._watch(NOON)
        monitor._watch(NOON + 60)  # told once a day
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith(
        "cpu: with nothing to serve, the workers use 5.0% of a core today, against 1.0%"
    )


def test_nginx_is_counted_across_its_reloads(monkeypatch):
    ticks = {10: (b"nginx", 100), 11: (b"python3", 900)}
    monkeypatch.setattr(cpu, "_ticks", lambda pid: ticks.get(pid))
    monkeypatch.setattr(cpu, "_pids", lambda: list(ticks))
    monkeypatch.setattr(cpu, "TICK", 100)
    nginx = Nginx()
    assert nginx.read(0) == 0  # what it used before isn't counted
    ticks[10] = (b"nginx", 150)
    assert nginx.read(5) == 0.5  # read again without a look through /proc
    del ticks[10]
    ticks[12] = (b"nginx", 30)  # a reload: a new worker
    assert nginx.read(61) == pytest.approx(0.8)


@pytest.mark.skipif(not os.path.exists("/proc/stat"), reason="Linux only")
def test_the_host_counters():
    busy, total, cores = cpu.host_times() or (0, 0, 0)
    assert 0 < busy < total and cores >= 1


def test_a_transfer_is_timed():
    m = CpuMonitor(":memory:")
    w = Watched(m, "t-1", "download")
    w.first, w.last = (0.0, 0.0), (0.5, 0.45)
    assert w._figures() == ("t-1", "download", 0.45, 0.5)
    w.last = w.first
    assert w._figures() is None  # one chunk, no time
    assert Watched(m, None, "upload").monitor is None  # no test, nothing recorded
    # A transfer's requests add up, each worker apart; under a second in all says nothing.
    for _ in range(4):
        m.record("t-2", "upload", 0.45, 0.5)
    m.record("t-2", "download", 0.9, 0.9)
    assert m.test("t-2") == {"upload": {"cpu": 0.9, "bound": True}}


def test_the_api(settings, tmp_path):
    settings.trusted_proxies = ["testclient"]
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/v1/cpu", headers=INSIDE).status_code == 404  # no internal_networks
        assert c.get("/api/v1/metrics", headers=INSIDE).status_code == 404
    settings.internal_networks = ["10.0.0.0/8"]
    with TestClient(create_app(settings)) as c:
        monitor: CpuMonitor = c.app.state.cpu
        monitor.sample(time.time() - 6)
        monitor.sample()
        assert c.get("/api/v1/cpu", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 404
        r = c.get("/api/v1/cpu", headers=INSIDE)
        assert r.status_code == 200
        body = r.json()
        assert body["now"]["workers"][0]["pid"] == os.getpid()
        assert body["busy"] is None and isinstance(body["days"], list)
        assert c.get("/api/v1/cpu?days=0", headers=INSIDE).json()["days"] == []
        text = c.get("/api/v1/metrics", headers=INSIDE).text
        assert f'texas2step_worker_cpu_seconds_total{{worker="{os.getpid()}"}}' in text
        assert "# TYPE texas2step_test_bytes_total counter" in text
        # A test's transfers, for its client.
        monitor.record("t-1", "download", 4.75, 5)
        monitor.record("t-1", "upload", 2.0, 5)
        assert c.get("/api/v1/tests/t-1").json() == {
            "test": "t-1",
            "download": {"cpu": 0.95, "bound": True},
            "upload": {"cpu": 0.4, "bound": False},
        }
        assert c.get("/api/v1/tests/none").json() == {"test": "none"}
        assert c.get("/api/v1/tests/a.b").status_code == 422


def test_a_busy_host_refuses_tests(settings):
    settings.cpu_busy_percent = 50
    with TestClient(create_app(settings)) as c:
        monitor: CpuMonitor = c.app.state.cpu
        assert c.get("/api/v1/down?bytes=10&test=early").status_code == 200
        monitor.busy = Busy(time.time(), 0.8)
        r = c.get("/api/v1/down?bytes=10&test=late")
        assert r.status_code == 503
        assert r.json()["message"] == "This server is busy. Try again in a few minutes."
        assert c.post("/api/v1/up?test=late", content=b"x").status_code == 503
        assert c.get("/api/v1/down?bytes=10&test=early").status_code == 200  # let in before
        assert c.get("/api/v1/ping").status_code == 200  # latency goes on


def test_the_tests_traffic_is_counted(client):
    work = client.app.state.cpu.work
    client.get("/api/v1/down?bytes=5000")
    client.post("/api/v1/up", content=b"x" * 3000)
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_bytes(b"y" * 100)
        ws.receive_bytes()
    assert (work.sent, work.received) == (5100, 3100)


class FakeCpu:
    def __init__(self) -> None:
        self.busy = True

    def refusing(self) -> bool:
        return self.busy

    def notice(self) -> dict[str, Any]:
        return {"detail": "busy", "message": "busy", "url": None}


class Channel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


async def test_a_busy_host_holds_video_calls_to_voice():
    echo = RtcEcho([], max_sessions=2, timeout=60)
    echo.cpu = FakeCpu()
    session = Session(id="x", pc=object())
    assert await echo.choose(session, "video") == {
        "op": "profile",
        "ok": False,
        "reason": "server busy",
        "detail": "busy",
        "message": "busy",
        "url": None,
    }
    assert (await echo.choose(session, "voice"))["ok"] is True
    # A Video call open as the host gets busy goes on as its Voice call.
    session.profile, session.control = "video", Channel()
    echo.sessions[session.id] = session
    await echo.hold()
    assert session.profile == "voice" and session.voice_only
    assert session.control.sent == [
        '{"op": "limit", "to": "voice", "detail": "busy", "message": "busy", "url": null}'
    ]
    await echo.hold()  # told once
    assert len(session.control.sent) == 1
    echo.cpu.busy = False
    assert (await echo.choose(session, "video"))["ok"] is True
    assert not session.voice_only


def test_the_settings():
    with pytest.raises(ValueError, match="cpu_busy_percent"):
        Settings(cpu_busy_percent=-1)
    with pytest.raises(ValueError, match="cpu_busy_percent"):
        Settings(cpu_busy_percent=101)
    with pytest.raises(ValueError, match="cpu_busy_seconds"):
        Settings(cpu_busy_seconds=5)
    assert Settings(cpu_busy_percent=80).cpu_busy_seconds == 30


def test_cpu_command(tmp_path: Path, capsys):
    db = tmp_path / "cpu.db"
    config = tmp_path / "config.toml"
    config.write_text(f'cpu_file = "{db}"\ncpu_busy_percent = 80\n')
    with pytest.raises(SystemExit, match="no figures yet"):
        cli.main(["cpu", "--config", str(config)])
    m = CpuMonitor(db)
    now = time.time()
    for i in range(7):
        write(
            m,
            41,
            now - 30 + i * 5,
            0.2 * i * 5,
            probes=820 * i * 5,
            sessions=5,
            busy=0.5 * i * 5,
            total=4 * i * 5,
            nginx=0.0,
        )
    m.close()
    cli.main(["cpu", "--config", str(config), "--days", "0"])
    out = capsys.readouterr().out
    assert "Workers     20% of a core: 41 20%" in out
    assert "Other work  7.5% of the host's CPU   (cpu_busy_percent = 80: tests let in)" in out
    assert "Serving     5 UDP sessions, 820 probes/s; tests 0.0 Mb/s" in out
    cli.main(["cpu", "--config", str(config), "--json", "--days", "0"])
    assert '"probes_per_s": 820.0' in capsys.readouterr().out
