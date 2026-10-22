"""Python measurement engine: measures *from this host to a texas2step server*.

Used by the CLI (``texas2step run URL``) and by the server-to-server functions under
``/api/v1/measure/*``. The browser client in ``web/src/engine`` implements the same
methodology against the same wire endpoints, so results are comparable.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from . import __version__, iface, payload
from .stats import bufferbloat_grade, summarize, summarize_throughput

API = "/api/v1"
BIN_MS = 100
PROBE = struct.Struct("<Id")  # sequence number, sender clock (ms)
USER_AGENT = f"texas2step-engine/{__version__}"

Progress = Callable[[str, dict[str, Any]], Awaitable[None] | None]


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url


def _ws_url(base: str) -> str:
    return "ws" + base.removeprefix("http") + f"{API}/ws"


def server_timing_ms(header: str | None, metric: str = "app") -> float:
    """Extract ``dur`` for ``metric`` from a Server-Timing header."""
    for entry in (header or "").split(","):
        name, *params = (p.strip() for p in entry.split(";"))
        if name == metric:
            for param in params:
                key, _, value = param.partition("=")
                if key == "dur":
                    with contextlib.suppress(ValueError):
                        return float(value)
    return 0.0


def _client(streams: int = 1, timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, read=timeout),
        limits=httpx.Limits(max_connections=streams + 2, max_keepalive_connections=streams + 2),
        headers={"user-agent": USER_AGENT},
    )


async def _emit(progress: Progress | None, phase: str, data: dict[str, Any]) -> None:
    if progress:
        result = progress(phase, data)
        if asyncio.iscoroutine(result):
            await result


# --- Latency ----------------------------------------------------------------------


async def measure_latency(
    target: str, count: int = 20, request_timeout: float = 2.0, progress: Progress | None = None
) -> dict[str, Any]:
    """Idle round-trip time over HTTP, server processing time removed via Server-Timing."""
    base = _base(target)
    samples: list[float] = []
    lost = 0
    async with _client(timeout=request_timeout) as client:
        await client.get(f"{base}{API}/ping")  # open + warm the connection
        for i in range(count):
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{base}{API}/ping", params={"r": i})
                r.raise_for_status()
            except httpx.HTTPError:
                lost += 1
                continue
            rtt = (time.perf_counter() - t0) * 1000 - server_timing_ms(
                r.headers.get("server-timing")
            )
            samples.append(max(rtt, 0.0))
            await _emit(progress, "latency", {"sample": rtt, "index": i})
    return summarize(samples, lost)


class _WsProber:
    """RTT probes over a dedicated WebSocket, used to measure latency under load."""

    def __init__(self, base: str, interval: float = 0.25) -> None:
        self.url = _ws_url(base)
        self.interval = interval
        self.samples: list[float] = []
        self.lost = 0

    async def run(self, stop: asyncio.Event) -> None:
        from websockets.asyncio.client import connect

        try:
            async with connect(self.url, open_timeout=5, max_size=1 << 16) as ws:
                seq = 0
                while not stop.is_set():
                    t0 = time.perf_counter()
                    await ws.send(PROBE.pack(seq, t0 * 1000))
                    try:
                        reply = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        if PROBE.unpack_from(reply)[0] == seq:
                            self.samples.append((time.perf_counter() - t0) * 1000)
                    except TimeoutError:
                        self.lost += 1
                    seq += 1
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.interval)
        except Exception:  # loaded latency is best effort
            return

    def summary(self) -> dict[str, Any] | None:
        return summarize(self.samples, self.lost) if self.samples else None


# --- Throughput -------------------------------------------------------------------


class _Bins:
    def __init__(self, start: float, seconds: float) -> None:
        self.start = start
        self.bytes = [0] * max(1, round(seconds * 1000 / BIN_MS))

    def add(self, n: int, now: float) -> None:
        i = int((now - self.start) * 1000 // BIN_MS)
        if 0 <= i < len(self.bytes):
            self.bytes[i] += n


async def _throughput(
    direction: str,
    target: str,
    seconds: float,
    streams: int,
    loaded_latency: bool,
    progress: Progress | None,
) -> dict[str, Any]:
    base = _base(target)
    per_stream = [0] * streams
    stop = asyncio.Event()
    prober = _WsProber(base) if loaded_latency else None

    async with _client(streams, timeout=seconds + 10) as client:
        await client.get(f"{base}{API}/ping")
        info = (await client.get(f"{base}{API}/info")).json()
        cap = int(info.get("limits", {}).get(f"max_{direction}_bytes", 1 << 30))

        start = time.perf_counter()
        deadline = start + seconds
        bins = _Bins(start, seconds)

        async def download(i: int) -> None:
            while time.perf_counter() < deadline:
                url = f"{base}{API}/down"
                async with client.stream("GET", url, params={"bytes": cap}) as r:
                    async for data in r.aiter_raw():
                        now = time.perf_counter()
                        if now >= deadline:
                            return
                        bins.add(len(data), now)
                        per_stream[i] += len(data)

        async def body(i: int) -> AsyncIterator[bytes]:
            sent = 0
            while sent < cap:
                now = time.perf_counter()
                if now >= deadline:
                    return
                n = min(payload.CHUNK // 4, cap - sent)
                yield payload.chunk(n, sent)
                # The previous chunk has been accepted by the transport once we resume.
                bins.add(n, time.perf_counter())
                per_stream[i] += n
                sent += n

        async def upload(i: int) -> None:
            while time.perf_counter() < deadline:
                r = await client.post(f"{base}{API}/up", content=body(i))
                r.raise_for_status()

        async def ticker() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.25)
                elapsed = time.perf_counter() - start
                done = min(len(bins.bytes), int(elapsed * 1000 // BIN_MS))
                window = bins.bytes[max(0, done - 5) : done]
                mbps = sum(window) * 8 / (len(window) * BIN_MS * 1000) if window else 0.0
                await _emit(progress, direction, {"elapsed": elapsed, "mbps": mbps})

        worker = download if direction == "download" else upload
        side = [asyncio.create_task(ticker())]
        if prober:
            side.append(asyncio.create_task(prober.run(stop)))
        try:
            results = await asyncio.gather(
                *(worker(i) for i in range(streams)), return_exceptions=True
            )
        finally:
            stop.set()
            await asyncio.gather(*side, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if len(errors) == streams:
            raise errors[0]

    elapsed = min(time.perf_counter() - start, seconds)
    complete = bins.bytes[: max(1, int(elapsed * 1000 // BIN_MS))]
    out = summarize_throughput(complete, BIN_MS, seconds)
    out |= {
        "direction": direction,
        "streams": streams,
        "per_stream_mbps": [b * 8 / (elapsed * 1e6) for b in per_stream],
        "loaded_latency": prober.summary() if prober else None,
    }
    return out


async def measure_download(
    target: str,
    seconds: float = 10.0,
    streams: int = 4,
    loaded_latency: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    return await _throughput("download", target, seconds, streams, loaded_latency, progress)


async def measure_upload(
    target: str,
    seconds: float = 10.0,
    streams: int = 4,
    loaded_latency: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    return await _throughput("upload", target, seconds, streams, loaded_latency, progress)


# --- Packet loss (UDP via WebRTC) -------------------------------------------------


async def measure_loss(
    target: str,
    packets: int = 500,
    interval_ms: float = 10.0,
    size: int = 64,
    wait_ms: float = 2000.0,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Send numbered UDP probes over an unreliable data channel and count the echoes."""
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    base = _base(target)
    size = max(size, PROBE.size)
    pad = bytes(size - PROBE.size)
    sent_at: dict[int, float] = {}
    rtts: list[float | None] = [None] * packets
    order: list[int] = []
    duplicates = 0
    loop = asyncio.get_running_loop()
    opened = asyncio.Event()
    all_back = asyncio.Event()
    control_reply: asyncio.Future[dict[str, Any]] = loop.create_future()

    async with _client() as client:
        info = (await client.get(f"{base}{API}/info")).json()
    ice = [RTCIceServer(urls=s["urls"]) for s in info.get("ice_servers", [])]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))  # never the implicit public STUN
    probe = pc.createDataChannel("probe", ordered=False, maxRetransmits=0)
    control = pc.createDataChannel("control")

    @probe.on("open")
    def _on_open() -> None:
        opened.set()

    @probe.on("message")
    def _on_message(message: bytes | str) -> None:
        nonlocal duplicates
        if not isinstance(message, bytes) or len(message) < PROBE.size:
            return
        seq, _ = PROBE.unpack_from(message)
        if seq >= packets or seq not in sent_at:
            return
        if rtts[seq] is not None:
            duplicates += 1
            return
        rtts[seq] = (time.perf_counter() * 1000) - sent_at[seq]
        order.append(seq)
        if len(order) == packets:
            all_back.set()

    @control.on("message")
    def _on_control(message: bytes | str) -> None:
        with contextlib.suppress(ValueError):
            reply = json.loads(message)
            if reply.get("op") == "stats" and not control_reply.done():
                control_reply.set_result(reply)

    try:
        await pc.setLocalDescription(await pc.createOffer())
        async with _client() as client:
            r = await client.post(
                f"{base}{API}/rtc/offer",
                json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            )
            r.raise_for_status()
            answer = r.json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
        await asyncio.wait_for(opened.wait(), timeout=10)

        start = time.perf_counter()
        for seq in range(packets):
            # Pace against the schedule rather than sleeping a fixed interval, so
            # event-loop overhead does not stretch the test.
            delay = start + seq * interval_ms / 1000 - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.perf_counter() * 1000
            sent_at[seq] = now
            probe.send(PROBE.pack(seq, now) + pad)
            if progress and seq % 25 == 0:
                await _emit(progress, "loss", {"sent": seq + 1})

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(all_back.wait(), wait_ms / 1000)

        control.send(json.dumps({"op": "stats"}))
        try:
            server = await asyncio.wait_for(control_reply, timeout=3)
        except TimeoutError:
            server = None
    finally:
        await pc.close()

    return loss_summary(rtts, order, duplicates, server, "webrtc", interval_ms, size)


def loss_summary(
    rtts: list[float | None],
    order: list[int],
    duplicates: int,
    server: dict[str, Any] | None,
    transport: str,
    interval_ms: float,
    size: int,
) -> dict[str, Any]:
    sent = len(rtts)
    received = sum(r is not None for r in rtts)
    out_of_order = sum(1 for a, b in itertools.pairwise(order) if b < a)
    up = down = None
    if server and sent:
        up = max(0.0, (sent - server["rx"]) / sent * 100)
        if server.get("tx"):
            down = max(0.0, (server["tx"] - received) / server["tx"] * 100)
    rtt_ordered = [r for r in rtts if r is not None]
    return {
        "transport": transport,
        "sent": sent,
        "received": received,
        "lost": sent - received,
        "loss_pct": (sent - received) / sent * 100 if sent else 0.0,
        "up_loss_pct": up,
        "down_loss_pct": down,
        "out_of_order": out_of_order,
        "duplicates": duplicates,
        "interval_ms": interval_ms,
        "size": size,
        "rtt": summarize(rtt_ordered, keep_samples=False),
        "packets": [None if r is None else round(r, 3) for r in rtts],
    }


# --- Full run ---------------------------------------------------------------------


async def run_all(
    target: str,
    seconds: float = 10.0,
    streams: int = 4,
    packets: int = 500,
    interval_ms: float = 10.0,
    loss: bool = True,
    progress: Progress | None = None,
    local: bool = False,
) -> dict[str, Any]:
    """A full test toward ``target``. With ``local``, the result also gives this host's
    own address on the connection the server saw, as the socket was bound, and the MAC
    of the NIC the kernel routes it out of (the CLI's; a server measuring for an API
    caller keeps them to itself)."""
    base = _base(target)
    started = datetime.now(UTC)
    async with _client() as client:
        info = (await client.get(f"{base}{API}/info")).json()
        r = await client.get(f"{base}{API}/whoami")
        whoami = r.json()
        stream = r.extensions.get("network_stream") if local else None
        mine = stream.get_extra_info("client_addr") if stream else None
        theirs = stream.get_extra_info("server_addr") if stream else None

    result: dict[str, Any] = {
        "version": 1,
        "tool": f"texas2step-cli/{__version__}",
        "started_at": started.isoformat(),
        "server": {**info.get("server", {}), "url": base, "version": info.get("version")},
        "client": {
            "ip": whoami.get("ip"),
            "family": whoami.get("family"),
            "protocol": whoami.get("http_version"),
            "user_agent": USER_AGENT,
        },
        "config": {
            "streams": streams,
            "seconds": seconds,
            "packets": packets if loss else 0,
            "interval_ms": interval_ms,
        },
    }
    if mine:
        result["client"]["local_ip"] = mine[0]
        if mac := await asyncio.to_thread(iface.mac_of, mine[0], theirs[0] if theirs else None):
            result["client"] |= {"mac": mac, "mac_source": "nic"}
    result["latency"] = await measure_latency(base, progress=progress)
    result["download"] = await measure_download(base, seconds, streams, progress=progress)
    result["upload"] = await measure_upload(base, seconds, streams, progress=progress)
    if loss and info.get("capabilities", {}).get("webrtc"):
        try:
            result["loss"] = await measure_loss(base, packets, interval_ms, progress=progress)
        except Exception as exc:
            result["loss"] = {"error": f"{type(exc).__name__}: {exc}"}
    result["bufferbloat"] = bufferbloat(result)
    result["duration_s"] = (datetime.now(UTC) - started).total_seconds()
    return result


def bufferbloat(result: dict[str, Any]) -> dict[str, Any]:
    idle = (result.get("latency") or {}).get("median")
    added: dict[str, float | None] = {}
    for direction in ("download", "upload"):
        loaded = ((result.get(direction) or {}).get("loaded_latency") or {}).get("median")
        added[direction] = loaded - idle if idle is not None and loaded is not None else None
    worst = max((v for v in added.values() if v is not None), default=None)
    return {
        "added_download_ms": added["download"],
        "added_upload_ms": added["upload"],
        "grade": bufferbloat_grade(worst),
    }
