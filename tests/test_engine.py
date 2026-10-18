"""End-to-end: the Python engine against a real server on loopback (HTTP, WebSocket, WebRTC)."""

import httpx

from texas2step import engine


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


async def test_run_all_shape(live_server):
    r = await engine.run_all(live_server, seconds=1, streams=1, packets=50, interval_ms=2)
    assert r["version"] == 1
    assert {"latency", "download", "upload", "loss", "bufferbloat"} <= set(r)
    assert r["bufferbloat"]["grade"] is not None
