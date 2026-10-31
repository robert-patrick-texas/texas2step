"""When the device is the limit: the kernel's figures for a download's connection
(tcpinfo.py), what a test's transfers add up to (cpu.py), and GET /api/v1/tests/{id}."""

from __future__ import annotations

import socket
import sys
import threading
import time
import uuid

import httpx
import pytest
from starlette.requests import Request

from texas2step.api.meta import connection_ends
from texas2step.config import Settings
from texas2step.cpu import CpuMonitor
from texas2step.tcpinfo import DIAG, Held, Info, Window

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="NETLINK_SOCK_DIAG")


def pair(family: int = socket.AF_INET) -> tuple[socket.socket, socket.socket, tuple]:
    """A connected pair on loopback: the server's end, the client's, and the ends tuple."""
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    with socket.socket(family) as listener:
        listener.bind((host, 0))
        listener.listen()
        client = socket.create_connection((host, listener.getsockname()[1]))
        server, _ = listener.accept()
    ends = (host, server.getsockname()[1], host, client.getsockname()[1])
    return server, client, ends


def send_while(server: socket.socket, window: Window, total: int) -> None:
    data = b"x" * 65536
    sent = 0
    while sent < total:
        handed = time.monotonic()
        sent += server.send(data)
        window.waited += time.monotonic() - handed
        window.moved()


@linux
def test_a_slow_reader_holds_the_connection_back():
    server, client, ends = pair(socket.AF_INET6)
    with server, client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)

        def read_slowly() -> None:
            try:
                while client.recv(65536):
                    time.sleep(0.004)
            except OSError:
                pass  # closed when the test is done

        threading.Thread(target=read_slowly, daemon=True).start()
        window = Window(ends)
        send_while(server, window, 30_000_000)
        held = window.held()
    assert held is not None and held.seconds > 0.3
    assert (held.idle + held.rwnd) / held.seconds > 0.5  # mostly the client's doing
    assert held.ready / held.seconds > 0.5  # while the sender waited with bytes ready
    assert held.rtt_n >= 2 and held.min_rtt_ms is not None


@linux
def test_lookups_find_ipv4_on_a_dual_stack_socket_and_nothing_else():
    with socket.socket(socket.AF_INET6) as listener:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind(("::", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            server, _ = listener.accept()
            with server:
                ends = ("127.0.0.1", port, "127.0.0.1", client.getsockname()[1])
                assert DIAG.lookup(ends) is not None
                mapped = ("::ffff:127.0.0.1", port, "::ffff:127.0.0.1", client.getsockname()[1])
                assert DIAG.lookup(mapped) is not None
    assert DIAG.lookup(("127.0.0.1", 1, "127.0.0.1", 2)) is None
    assert DIAG.lookup(("127.0.0.1", 1, "::1", 2)) is None
    assert DIAG.lookup(("not an address", 1, "127.0.0.1", 2)) is None
    window = Window(("127.0.0.1", 1, "127.0.0.1", 2))
    window.moved()
    assert window.ends is None and window.held() is None  # stops asking after a miss


class Fake:
    """Lookups that answer what a test queues up, as a connection's figures over time."""

    available = True

    def __init__(self, *infos: Info | None) -> None:
        self.infos = list(infos)

    def lookup(self, ends) -> Info | None:
        return self.infos.pop(0) if self.infos else None


def info(at: float, busy: float, rwnd: float = 0.0, rtt: int = 20_000) -> Info:
    return Info(at, int(busy * 1e6), int(rwnd * 1e6), 0, rtt, 15_000)


def drive(window: Window, lookups: int) -> None:
    for _ in range(lookups):
        window._next = 0.0  # each moved() looks, as a quarter second has gone by
        window.moved()


def test_a_connection_the_client_closed_keeps_the_figures_it_had():
    # The kernel sets a reset connection's counters back to 0 (and says TCP_CLOSE, which
    # lookup() refuses): the transfer's figures are those from before.
    fake = Fake(info(0.0, 0.0), info(1.0, 0.9), info(2.0, 1.8), info(2.1, 0.0))
    window = Window(("192.0.2.1", 443, "192.0.2.7", 50000), diag=fake)  # ty: ignore[invalid-argument-type]
    drive(window, 3)
    got = window.held()
    assert got is not None and got.seconds == 2.0
    assert got.idle == pytest.approx(0.2) and got.rwnd == 0.0


def test_over_http1_nothing_in_flight_is_the_servers_doing():
    # A slow HTTP/1.1 client fills its receive window; an idle connection there means the
    # server's side had nothing to send.
    lookups = (info(0.0, 0.0), info(1.0, 0.5, rwnd=0.2), info(2.0, 1.0, rwnd=0.4))
    h2 = Window(("192.0.2.1", 443, "192.0.2.7", 50000), diag=Fake(*lookups))  # ty: ignore[invalid-argument-type]
    h1 = Window(("192.0.2.1", 443, "192.0.2.7", 50000), False, Fake(*lookups))  # ty: ignore[invalid-argument-type]
    for window in (h2, h1):
        drive(window, 3)
    got2, got1 = h2.held(), h1.held()
    assert got2 is not None and got1 is not None
    assert (got2.idle, got2.rwnd) == (pytest.approx(1.0), pytest.approx(0.4))
    assert (got1.idle, got1.rwnd) == (0.0, pytest.approx(0.4))


def held(seconds=10.0, idle=0.0, rwnd=0.0, ready=None, rtt=40.0, n=40) -> Held:
    return Held(seconds, idle, rwnd, rtt * n, n, 30.0, seconds if ready is None else ready)


def test_a_transfer_held_back_reads_device_bound():
    m = CpuMonitor(":memory:")
    m.record("t", "download", 0.5, 10.0)
    m.record_window("t", "download", held(idle=2.5, rwnd=0.7))
    m.record_window("t", "download", held(idle=0.0, rwnd=0.0, rtt=50.0))  # another request
    m.record("t", "one_stream", 0.4, 10.0)
    m.record_window("t", "one_stream", held(idle=0.5))
    got = m.test("t", 10.0)
    assert got["download"]["window"] == {
        "held": 0.16,
        "idle": 0.125,
        "rwnd": 0.035,
        "seconds": 20.0,
        "rtt_ms": 45.0,
        "min_rtt_ms": 30.0,
        "ready": 1.0,
        "bound": True,
    }
    assert got["one_stream"]["window"]["held"] == 0.05
    assert got["one_stream"]["window"]["bound"] is False  # under 10 %
    assert m.test("t", 20.0)["download"]["window"]["bound"] is False
    assert m.test("t", 0.0)["download"]["window"]["bound"] is False  # 0: never
    assert "upload" not in got


def test_a_server_that_couldnt_keep_up_isnt_the_clients_fault():
    m = CpuMonitor(":memory:")
    m.record("cpu", "download", 9.5, 10.0)  # a full core: "Server CPU-bound" instead
    m.record_window("cpu", "download", held(idle=5.0))
    m.record_window("slow", "download", held(idle=5.0, ready=2.0))  # bytes ready 20 %
    m.record_window("short", "download", held(seconds=0.5, idle=0.5))
    assert m.test("cpu", 10.0)["download"]["bound"] is True
    assert m.test("cpu", 10.0)["download"]["window"]["bound"] is False
    slow = m.test("slow", 10.0)["download"]["window"]
    assert slow["held"] == 0.5 and slow["ready"] == 0.2 and slow["bound"] is False
    assert m.test("short", 10.0) == {}  # under a second: no figure


def request(headers: dict[str, str], client=("127.0.0.1", 40000), server=("127.0.0.1", 8080)):
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.settings = Settings()
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "headers": raw, "client": client, "server": server, "app": app}
    return Request(scope)


def test_the_connection_is_nginxs_behind_it_and_our_own_without():
    nginx = {
        "X-Forwarded-For": "2001:db8::7",
        "X-Client-Port": "51234",
        "X-Server-Addr": "2001:db8::1",
        "X-Server-Port": "443",
        "X-Client-Protocol": "HTTP/2.0",
    }
    assert connection_ends(request(nginx)) == ("2001:db8::1", 443, "2001:db8::7", 51234)
    mapped = nginx | {"X-Forwarded-For": "::ffff:192.0.2.7", "X-Server-Addr": "::ffff:192.0.2.1"}
    assert connection_ends(request(mapped)) == ("192.0.2.1", 443, "192.0.2.7", 51234)
    assert connection_ends(request(nginx | {"X-Client-Protocol": "HTTP/3.0"})) is None
    assert (
        connection_ends(request({k: v for k, v in nginx.items() if k != "X-Client-Port"})) is None
    )
    # A client of its own, or one that isn't a trusted proxy: this socket's ends.
    assert connection_ends(request({})) == ("127.0.0.1", 8080, "127.0.0.1", 40000)
    stranger = request(nginx, client=("198.51.100.9", 40000))
    assert connection_ends(stranger) == ("127.0.0.1", 8080, "198.51.100.9", 40000)
    assert connection_ends(request({}, client=("testclient", 50000))) is None


@linux
def test_the_server_times_a_slow_download(live_server):
    test = uuid.uuid4().hex[:12]
    url = f"{live_server}/api/v1/down"
    with httpx.Client(timeout=30) as c:
        with c.stream("GET", url, params={"bytes": 1_000_000_000, "test": test}) as r:
            start = time.monotonic()
            for _ in r.iter_raw(65536):
                time.sleep(0.003)  # a device that reads ~20 MB/s at most
                if time.monotonic() - start > 2.5:
                    break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # recorded in the background as it ends
            parts = c.get(f"{live_server}/api/v1/tests/{test}").json()
            if "window" in parts.get("download", {}):
                break
            time.sleep(0.1)
    window = parts["download"]["window"]
    assert window["seconds"] >= 1
    assert window["held"] > 0.3 and window["ready"] > 0.5
    assert window["bound"] is True
    assert window["rtt_ms"] is not None
