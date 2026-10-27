"""Active clients: sessions told apart by the id the page sends (clients.py), texas2step
clients, and GET /api/v1/clients."""

import asyncio
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from texas2step import cli, clients
from texas2step.app import create_app
from texas2step.clients import IDLE, Clients, Seen, duration, program
from texas2step.config import Settings

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
EDGE = CHROME + " Edg/141.0.3537.71"
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:157.0) Gecko/20100101 Firefox/157.0"
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
INSIDE = {"X-Forwarded-For": "10.1.2.3"}


def test_programs_from_user_agents():
    assert program(CHROME) == "Chrome 141 on Windows"
    assert program(EDGE) == "Edge 141 on Windows"
    assert program(FIREFOX) == "Firefox 157 on Linux"
    assert program(IPHONE) == "Safari 18 on iPhone"
    assert program("texas2step-engine/2.5.0") == "texas2step 2.5.0"
    assert program("curl/8.5.0") == "curl 8.5.0"
    assert program("") == "unknown"
    assert program("evil\x1b[2J\nline") == "evil?[2J?line"  # nothing for a terminal to run


def test_durations():
    assert [duration(s) for s in (8, 59.4, 61, 3599, 3600, 3900)] == [
        "8s",
        "59s",
        "1 min",
        "59 min",
        "1 h",
        "1 h 5 min",
    ]


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    settings.trusted_proxies = ["testclient"]  # so X-Forwarded-For names the visitor
    return settings


def flush(c: TestClient, now: float | None = None) -> None:
    c.portal.call(c.app.state.clients.flush, now)  # ty: ignore[unresolved-attribute]


def test_two_browsers_on_one_address_are_two_sessions(app_settings):
    with TestClient(create_app(app_settings)) as c:
        ip = {"X-Forwarded-For": "203.0.113.7"}
        a = {"session": "k7q2m9xd", "page": "monitor"}
        b = {"session": "p3x8wq2n", "page": "diag"}
        c.get("/api/v1/ping", params=a, headers={**ip, "User-Agent": CHROME})
        c.get("/api/v1/down", params={**a, "bytes": 5000}, headers={**ip, "User-Agent": CHROME})
        c.get("/api/v1/diag/http", params=b, headers={**ip, "User-Agent": FIREFOX})
        c.get("/api/v1/ping", params=b, headers={"X-Forwarded-For": "2001:db8::7"})
        c.get("/api/v1/health", params=b, headers=ip)  # Monitor asking a quiet server: its
        c.get("/api/v1/health", headers={"X-Forwarded-For": "192.0.2.5"})  # monitoring's: none
        flush(c)
        sessions = clients.read(app_settings.clients_file).active
    assert [s.session for s in sessions] == ["k7q2m9xd", "p3x8wq2n"]
    first, second = sessions
    assert first.page == "monitor" and program(first.agent) == "Chrome 141 on Windows"
    assert first.addresses == ["203.0.113.7"]
    assert first.sent >= 5000
    assert clients.describe(first.doing) == ["Download, 1 stream", "Latency"]  # just done
    assert second.page == "diag"
    assert set(second.addresses) == {"203.0.113.7", "2001:db8::7"}  # one page, both families
    assert clients.describe(second.doing) == ["Latency", "HTTP checks"]


def test_requests_without_an_id_go_by_address_and_program(app_settings):
    with TestClient(create_app(app_settings)) as c:
        for agent in ("curl/8.5.0", "curl/8.5.0", "Wget/1.24"):
            c.get("/api/v1/ping", headers={"X-Forwarded-For": "203.0.113.9", "User-Agent": agent})
        c.get("/api/v1/ping", params={"session": "a b"}, headers={"User-Agent": "curl/8.5.0"})
        flush(c)
        sessions = clients.read(app_settings.clients_file).active
    assert sorted((s.label, program(s.agent), s.addresses[0]) for s in sessions) == [
        ("(no id)", "Wget 1.24", "203.0.113.9"),
        ("(no id)", "curl 8.5.0", "203.0.113.9"),
        ("(no id)", "curl 8.5.0", "testclient"),  # a malformed id is no id
    ]


def visit(worker: Clients, key: str, activity: str = "page", **kw: str) -> Seen:
    """A request from session ``key`` starts and ends at ``worker``."""
    page, agent, ip = kw.get("page", "test"), kw.get("agent", CHROME), kw.get("ip", "203.0.113.7")
    seen = worker.begin(key, key, page, agent, ip, activity)
    assert seen is not None
    worker.end(seen, activity)
    return seen


def test_the_journal_hears_each_start_page_and_end_once(tmp_path, caplog):
    path = str(tmp_path / "c.db")
    one, two = Clients(path), Clients(path)
    two.worker = one.worker + 1  # the service's second worker, sharing the file
    for worker in (one, two):
        visit(worker, "k7q2m9xd", "download").sent += 1_000_000
    caplog.set_level(logging.INFO, logger="texas2step.clients")
    asyncio.run(one.flush())
    asyncio.run(two.flush())
    started = [r.getMessage() for r in caplog.records]
    assert started == ["clients: k7q2m9xd started: Test, Chrome 141 on Windows, from 203.0.113.7"]
    (both,) = clients.read(path).active
    assert both.sent == 2_000_000  # each worker's share

    caplog.clear()
    visit(two, "k7q2m9xd", page="monitor")
    asyncio.run(two.flush())
    asyncio.run(one.flush())
    assert [r.getMessage() for r in caplog.records] == ["clients: k7q2m9xd now on Monitor"]

    caplog.clear()
    later = time.time() + IDLE + 5
    asyncio.run(one.flush(later))
    asyncio.run(two.flush(later))
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith("clients: k7q2m9xd ended: Monitor, Chrome 141 on Windows")
    assert line.endswith("moved 2 MB")
    board = clients.read(path, later)
    assert board.active == []
    (ended,) = board.ended  # kept for the hour
    assert (ended.session, ended.page, ended.sent) == ("k7q2m9xd", "monitor", 2_000_000)
    assert not one.seen and not two.seen  # forgotten once it ended everywhere
    assert clients.read(path, later + clients.HISTORY).ended == []


def test_a_session_quiet_on_one_worker_goes_on_while_the_other_serves_it(tmp_path):
    path = str(tmp_path / "c.db")
    one, two = Clients(path), Clients(path)
    two.worker = one.worker + 1
    seen = visit(one, "k7q2m9xd", page="monitor")
    seen.sent = 500
    asyncio.run(one.flush())
    seen.last -= IDLE + 5  # quiet here...
    two.begin("k7q2m9xd", "k7q2m9xd", "monitor", CHROME, "203.0.113.7", "websocket")
    asyncio.run(two.flush())  # ...while the other worker holds its echo socket open
    asyncio.run(one.flush())
    (s,) = clients.read(path).active
    assert s.sent == 500  # its bytes here still count
    assert clients.describe(s.doing) == ["WebSocket echo"]


def test_only_the_sessions_that_changed_are_written(tmp_path):
    path = str(tmp_path / "c.db")
    worker = Clients(path)
    for n in range(50):
        visit(worker, f"page{n:04d}")
    assert len(worker._rows(time.time())) == 50
    visit(worker, "page0007")
    assert [r["key"] for r in worker._rows(time.time())] == ["page0007"]
    assert worker._rows(time.time()) == []  # nothing new: nothing to write


def test_the_list_keeps_clients_max_sessions_and_warns_once_when_full(tmp_path, caplog):
    path = str(tmp_path / "c.db")
    one, two = Clients(path, limit=10), Clients(path, limit=10)
    two.worker = one.worker + 1
    caplog.set_level(logging.INFO, logger="texas2step.clients")
    t0 = time.time()
    for n in range(6):  # six pages that came and went: the last hour's
        visit(one, f"gone{n:04d}", ip="198.51.100.4")
    asyncio.run(one.flush(t0))
    asyncio.run(one.flush(t0 + IDLE + 1))
    assert len(clients.read(path, t0 + IDLE + 1).ended) == 6

    for n in range(9):  # a reload storm from one address
        visit(one, f"load{n:04d}", ip="203.0.113.9")
    visit(two, "desk0001", ip="10.1.2.3")
    asyncio.run(one.flush())
    asyncio.run(two.flush())
    asyncio.run(one.flush())  # each worker goes by the count at its last flush
    board = clients.read(path)
    assert len(board.active) == 10
    assert board.ended == []  # active ones come first: no room left for the hour's
    assert not board.full

    caplog.clear()
    for n in [0, 1, 2, 3, 4, 0, 0]:  # more: served, not listed, each counted once
        assert (
            one.begin(f"more{n:04d}", f"more{n:04d}", "test", CHROME, "203.0.113.9", "page") is None
        )
    assert visit(one, "load0003", ip="203.0.113.9").key == "load0003"  # listed: carries on
    two.begin("load0004", "load0004", "test", CHROME, "203.0.113.9", "page")  # ...on either worker
    asyncio.run(one.flush())
    asyncio.run(two.flush())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "clients: the list is full: 10 sessions active, clients_max = 10; 5 new ones weren't "
        "listed. The most sessions come from 203.0.113.9 (9), 10.1.2.3 (1)"
    ]
    board = clients.read(path)
    assert len(board.active) == 10 and board.full
    one.begin("more0009", "more0009", "test", CHROME, "203.0.113.9", "page")
    asyncio.run(one.flush())
    asyncio.run(two.flush())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # once for both workers, and not again for ten minutes


def test_the_file_holds_the_list_to_clients_max_exactly(tmp_path, caplog):
    path = str(tmp_path / "c.db")
    one, two = Clients(path, limit=10), Clients(path, limit=10)  # each thinks the room its own
    two.worker = one.worker + 1
    for n in range(8):
        visit(one, f"a{n:07d}")
        visit(two, f"b{n:07d}")
    caplog.set_level(logging.INFO, logger="texas2step.clients")
    asyncio.run(one.flush())
    asyncio.run(two.flush())
    assert len(clients.read(path).active) == 10  # the second to write got room for two
    assert len(two.seen) == 2
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "10 sessions active, clients_max = 10; 6 new ones weren't listed" in warning
    assert two.begin("b0000007", "b0000007", "test", CHROME, "203.0.113.7", "page") is None
    assert len([r for r in caplog.records if "started" in r.getMessage()]) == 10


def test_ended_sessions_make_room_oldest_first(tmp_path):
    path = str(tmp_path / "c.db")
    worker = Clients(path, limit=10)
    t0 = time.time()
    for batch in range(3):  # three waves of five pages, a minute and a half apart
        now = t0 + batch * 90
        for n in range(5):
            seen = visit(worker, f"wave{batch}{n:03d}")
            seen.first = seen.last = now
        asyncio.run(worker.flush(now))
        asyncio.run(worker.flush(now + IDLE + 1))  # ended, into the last hour's list
    ended = clients.read(path, t0 + 2 * 90 + IDLE + 1).ended
    assert len(ended) == 10  # clients_max: the oldest wave made room
    assert {(s.session or "")[:5] for s in ended} == {"wave1", "wave2"}
    assert (ended[0].session or "").startswith("wave2")  # the latest first


def test_the_list_is_for_internal_networks_only(app_settings):
    with TestClient(create_app(app_settings)) as c:
        assert c.get("/api/v1/clients", headers=INSIDE).status_code == 404  # none set
    app_settings.internal_networks = ["10.0.0.0/8"]
    with TestClient(create_app(app_settings)) as c:
        visitor = {"session": "k7q2m9xd", "page": "test"}
        c.get("/api/v1/ping", params=visitor, headers={"X-Forwarded-For": "10.9.9.9"})
        outside = {"X-Forwarded-For": "203.0.113.9"}
        assert c.get("/api/v1/clients", headers=outside).status_code == 404
        flush(c)
        r = c.get("/api/v1/clients", headers=INSIDE)
    assert r.status_code == 200
    (one,) = r.json()["clients"]  # the desk's own request isn't listed
    assert one["session"] == "k7q2m9xd" and one["page"] == "test"
    assert one["addresses"] == ["10.9.9.9"]
    assert one["doing"] == ["Latency"]


def test_clients_command(tmp_path, capsys):
    db = tmp_path / "c.db"
    config = tmp_path / "config.toml"
    config.write_text(f'clients_file = "{db}"\n')
    cli.main(["clients", "--config", str(config)])
    assert capsys.readouterr().out == "No clients active in the last 60s.\n"
    assert not db.exists()  # the service makes it

    worker = Clients(str(db))
    a = worker.begin("k7q2m9xd", "k7q2m9xd", "monitor", CHROME, "203.0.113.7", "websocket")
    assert a is not None
    a.addresses["2001:db8::7"] = a.first - 1
    run = ("d9f4m2kq", "d9f4m2kq", "cli", "texas2step-engine/2.5.0", "198.51.100.4")
    worker.begin(*run, "download")
    worker.begin(*run, "download")
    asyncio.run(worker.flush())
    cli.main(["clients", "--config", str(config)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "2 active in the last 60s: 1 page, 1 command-line run."
    assert " ".join(lines[2].split()) == "Session Address Program Page Doing now For Moved"
    words = " ".join(lines[3].split())
    assert words.startswith("k7q2m9xd 203.0.113.7 Chrome 141 on Windows Monitor WebSocket echo")
    assert lines[4].split() == ["2001:db8::7"]  # its other address, on a line of its own
    assert "texas2step 2.5.0" in lines[5] and "CLI" in lines[5]
    assert "Download, 2 streams" in lines[5]

    cli.main(["clients", "--json", "--config", str(config)])
    out = json.loads(capsys.readouterr().out)
    assert [c["session"] for c in out["clients"]] == ["k7q2m9xd", "d9f4m2kq"]
    assert out["clients"][1]["doing"] == ["Download, 2 streams"]
    assert out["clients"][1]["program"] == "texas2step 2.5.0"
    assert out["ended"] == [] and out["clients_max"] == 1000 and not out["full"]


def test_clients_command_lists_the_last_hours_ended_sessions(tmp_path, capsys):
    db = tmp_path / "c.db"
    config = tmp_path / "config.toml"
    config.write_text(f'clients_file = "{db}"\n')
    worker = Clients(str(db))
    t0 = time.time() - 600
    for n in range(25):
        seen = visit(worker, f"page{n:04d}")
        seen.first, seen.last = t0 + n, t0 + n + 30
    asyncio.run(worker.flush(t0 + 300))
    cli.main(["clients", "--config", str(config)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "No clients active in the last 60s."
    assert lines[2] == "25 ended in the last hour, the latest first:"
    assert " ".join(lines[4].split()) == "Session Address Program Page Last seen For Moved"
    words = " ".join(lines[5].split())
    assert words.startswith("page0024 203.0.113.7 Chrome 141 on Windows Test 9 min ago 30s")
    assert len([line for line in lines if line.startswith("  page")]) == 20
    assert lines[-1] == "5 more: --history lists them all."
    cli.main(["clients", "--history", "--config", str(config)])
    lines = capsys.readouterr().out.splitlines()
    assert len([line for line in lines if line.startswith("  page")]) == 25

    config.write_text(f'clients_file = "{tmp_path / "run" / "c.db"}"\n')
    cli.main(["clients", "--config", str(config)])
    assert (
        capsys.readouterr().out
        == f"No clients: the service makes {tmp_path / 'run'} as it starts.\n"
    )
