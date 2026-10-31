"""End-to-end: the Python engine against a real server on loopback (HTTP, WebSocket, WebRTC)."""

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from texas2step import access, engine, payload
from texas2step.app import create_app
from texas2step.config import Location, Settings


def test_upload_spans_walk_the_whole_pool():
    n = payload.CHUNK // 4  # the CLI's upload chunk
    pool = payload.CHUNK * len(payload._POOL)
    spans = [payload.span(offset, n) for offset in range(0, pool, n)]
    assert len(set(spans)) == len(spans)  # no span repeats before the pool's end
    assert b"".join(spans) == b"".join(payload._POOL)
    assert payload.span(pool, n) == spans[0]  # then it starts again
    assert payload.span(payload.CHUNK - 10, 20) == payload._POOL[0][-10:] + payload._POOL[1][:10]


async def test_latency(live_server):
    r = await engine.measure_latency(live_server, count=5)
    assert r["count"] == 5 and r["lost"] == 0
    assert 0 <= r["median"] < 100


async def test_download_and_upload(live_server):
    down = await engine.measure_download(live_server, seconds=1.5, streams=2)
    assert down["direction"] == "download"
    assert down["mbps"] > 1
    assert len(down["per_stream_mbps"]) == 2
    assert down["loaded_latency"] is not None

    up = await engine.measure_upload(live_server, seconds=1.5, streams=2)
    assert up["mbps"] > 1
    assert up["bytes"] > 0


async def test_udp_loss_over_webrtc(live_server):
    r = await engine.measure_loss(live_server, packets=100, interval_ms=2, wait_ms=1500)
    assert r["transport"] == "webrtc"
    assert r["sent"] == 100
    assert r["received"] >= 95  # loopback; allow for scheduler hiccups
    assert r["up_loss_pct"] is not None
    assert len(r["packets"]) == 100


async def test_server_side_measure_endpoint(live_server):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{live_server}/api/v1/measure/latency", json={"target": live_server, "count": 3}
        )
    assert r.status_code == 200
    assert r.json()["count"] == 3


def _serve(settings: Settings, host: str) -> tuple[uvicorn.Server, threading.Thread, str]:
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host=host, port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    return server, thread, f"http://{host}:{server.servers[0].sockets[0].getsockname()[1]}"


def _own_address() -> str | None:
    """This computer's address on its way out: a server there sees this computer's own
    requests come from it, not from loopback, which is always inside."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # chooses the route; sends nothing
        except OSError:
            return None
        return s.getsockname()[0]


@pytest.fixture
def code_server(tmp_path) -> Iterator[tuple[str, str]]:
    """A server that asks everyone for the access code, this computer included: its
    address, and its key's file."""
    host = _own_address()
    if host is None or host.startswith("127."):
        pytest.skip("no address but loopback, which needs no code")
    key = tmp_path / "access.key"
    access.write_key(key, access.new_key())
    settings = Settings(
        access_code="daily",
        access_key_file=str(key),
        access_phrases_file=str(tmp_path / "phrases.toml"),
        access_lockouts_file=str(tmp_path / "access.db"),
        results_path=str(tmp_path / "r.db"),
        clients_file=str(tmp_path / "c.db"),
    )
    server, thread, url = _serve(settings, host)
    assert httpx.get(f"{url}/api/v1/ping").status_code == 401  # outside: it asks
    yield url, str(key)
    server.should_exit = True
    thread.join(timeout=10)


def test_a_server_with_the_same_key_measures_another_with_a_pass(code_server, tmp_path):
    target, key = code_server
    other_key = tmp_path / "other.key"
    access.write_key(other_key, access.new_key())

    def measure(**settings: object) -> httpx.Response:
        app = create_app(
            Settings(
                results_path=str(tmp_path / "m.db"),
                clients_file=str(tmp_path / "mc.db"),
                access_lockouts_file=str(tmp_path / "m-access.db"),
                access_phrases_file=str(tmp_path / "m-phrases.toml"),
                internal_networks=["10.0.0.0/8"],
                trusted_proxies=["testclient"],
                **settings,  # ty: ignore[invalid-argument-type]
            )
        )
        with TestClient(app) as c:
            return c.post(
                "/api/v1/measure/latency",
                json={"target": target, "count": 3},
                headers={"X-Forwarded-For": "10.1.2.3"},
            )

    def answered(r: httpx.Response) -> int:
        """How many of the 3 pings the target answered."""
        assert r.status_code == 200, r.text
        return r.json()["count"]

    def refused(r: httpx.Response) -> bool:
        """The target refused the test, and the answer says so: not pings lost."""
        return r.status_code == 502 and r.json()["detail"].startswith("target answered 401")

    here = [Location(id="austin", name="Austin", url=target)]
    # Its own pass gets it in, though the target counts it as outside.
    assert answered(measure(access_code="daily", access_key_file=key, locations=here)) == 3
    assert answered(measure(access_key_file=key, locations=here)) == 3  # codes off here: the key
    assert refused(measure(access_key_file=str(other_key), locations=here))  # another key
    # Not in [[locations]], only let in by allow_any_target: no pass goes there.
    assert refused(measure(access_key_file=key, allow_any_target=True))


async def test_latency_toward_a_server_that_refuses_says_so(code_server):
    target, _ = code_server
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await engine.measure_latency(target, count=3)  # no pass: every ping answered 401
    assert refused.value.response.status_code == 401
    unreachable = f"http://{httpx.URL(target).host}:9"  # nothing listens: no answer
    with pytest.raises(httpx.TransportError):
        await engine.measure_latency(unreachable, count=3, request_timeout=1)


async def test_run_all_shape(live_server):
    r = await engine.run_all(
        live_server, seconds=1, streams=1, packets=50, interval_ms=2, local=True
    )
    assert r["version"] == 1
    assert {"latency", "download", "upload", "loss", "bufferbloat"} <= set(r)
    assert r["bufferbloat"]["grade"] is not None
    assert r["responsiveness"]["rpm"] > 0
    # streams=1: no second, one-stream download
    assert r["config"]["one_stream"] is False and "one_stream" not in r
    # A call's quality comes from the packet-loss test's round trips.
    assert ("call" in r) == bool(r["loss"].get("packets"))
    # The CLI's own end of the connection the server saw; loopback has no MAC.
    assert r["client"]["local_ip"] == r["client"]["ip"] == "127.0.0.1"
    assert "mac" not in r["client"]


async def test_run_all_measures_one_stream_and_responsiveness(live_server):
    r = await engine.run_all(live_server, seconds=1.5, streams=2, packets=40, interval_ms=5)
    assert r["config"]["one_stream"] is True
    assert r["one_stream"]["streams"] == 1 and r["one_stream"]["mbps"] > 0
    assert r["one_stream"]["loaded_latency"] is None
    # New connections, timed step by step, during each transfer; one idle before them.
    assert r["latency"]["setup_ms"] > 0
    for direction in ("download", "upload"):
        c = r[direction]["connections"]
        assert c["tls"] is False and "tls_ms" not in c
        assert len(c["setup_ms"]) == len(c["tcp_ms"]) == len(c["http_ms"]) >= 1
    assert r["responsiveness"]["basis"] == "ippm"
    assert r["responsiveness"]["download"]["loaded_on"] == "own connection"
    # The server timed its CPU for each transfer, the one stream's apart. Client and server
    # share this process here, so the figure is theirs together.
    assert all(r[part]["server_cpu"] > 0 for part in ("download", "one_stream", "upload"))
    no_one = await engine.run_all(live_server, seconds=1, streams=2, loss=False, one_stream=False)
    assert "one_stream" not in no_one and no_one["config"]["one_stream"] is False


@pytest.fixture
def billed_server(tmp_path) -> Iterator[str]:
    """A server whose traffic is billed: one stream is off unless a test asks for it."""
    settings = Settings(results_path=str(tmp_path / "r.db"), default_one_stream=False)
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=10)


async def test_one_stream_follows_the_servers_default(billed_server):
    r = await engine.run_all(billed_server, seconds=1, streams=2, loss=False)
    assert r["config"]["one_stream"] is False and "one_stream" not in r
    asked = await engine.run_all(billed_server, seconds=1, streams=2, loss=False, one_stream=True)
    assert asked["config"]["one_stream"] is True and asked["one_stream"]["mbps"] > 0
