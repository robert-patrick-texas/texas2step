"""Python measurement engine: measures *from this host to a texas2step server*.

Used by the CLI (``texas2step run URL``) and by the server-to-server functions under
``/api/v1/measure/*``. The browser client in ``web/src/engine`` implements the same
methodology against the same wire endpoints, so results are comparable.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import json
import os
import secrets
import ssl
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from . import __version__, iface, payload, quality
from .stats import bufferbloat_grade, summarize, summarize_throughput
from .store import ID_LETTERS

API = "/api/v1"
BIN_MS = 100
PROBE = struct.Struct("<Id")  # sequence number, sender clock (ms)
STAMP = struct.Struct("<dI")  # with stamps on: the server's clock on arrival (ms), its count
PROBE_BYTES = 200  # at least a voice packet: a path that can't carry these can't carry calls
USER_AGENT = f"texas2step-engine/{__version__}"

Progress = Callable[[str, dict[str, Any]], Awaitable[None] | None]
# An access pass (docs/access.md), sent as ?pass= with every request; see unlock().
PASS: contextvars.ContextVar[str] = contextvars.ContextVar("texas2step_pass", default="")
# The test the download and upload requests belong to, sent as ?test=: a traffic limit the
# server reaches while it runs doesn't stop it (docs/traffic.md). run_all sets one.
TEST: contextvars.ContextVar[str] = contextvars.ContextVar("texas2step_test", default="")


# Who is asking, sent as ?session= and ?page= with every request, so the server's
# texas2step clients can tell runs apart (docs/clients.md): one id for each run of the
# command line, and "server" with a new id for each test the server runs (/measure).
def new_session() -> str:
    return "".join(secrets.choice(ID_LETTERS) for _ in range(8))


RUN = new_session()
SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("texas2step_session", default=RUN)
PAGE: contextvars.ContextVar[str] = contextvars.ContextVar("texas2step_page", default="cli")


def _params() -> dict[str, str]:
    """What every request carries: the session, and the pass if there is one."""
    params = {"session": SESSION.get(), "page": PAGE.get()}
    return params | {"pass": PASS.get()} if PASS.get() else params


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url


def _ws_url(base: str) -> str:
    return "ws" + base.removeprefix("http") + f"{API}/ws?{urlencode(_params())}"


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
        params=_params(),
    )


async def unlock(target: str, code: str | None) -> str:
    """A pass for ``code`` from the server at ``target``: "" if it asks for no code.
    PermissionError if it asks for one and ``code`` is missing or wrong."""
    url = f"{_base(target)}{API}/access"
    async with _client() as client:
        r = await client.get(url)
        if r.status_code == 404 or not r.json().get("required"):
            return ""  # a server from before access codes, or one that doesn't ask
        if not code:
            raise PermissionError("asks for an access code: add --code, or set TEXAS2STEP_CODE")
        r = await client.post(url, json={"code": code})
        if r.status_code != 200:
            json_body = "json" in r.headers.get("content-type", "")
            detail = r.json().get("detail") if json_body else None
            raise PermissionError(detail or f"{r.status_code} {r.reason_phrase}")
        return r.json()["pass"]


async def _emit(progress: Progress | None, phase: str, data: dict[str, Any]) -> None:
    if progress:
        result = progress(phase, data)
        if asyncio.iscoroutine(result):
            await result


# --- Latency ----------------------------------------------------------------------


async def measure_latency(
    target: str, count: int = 20, request_timeout: float = 2.0, progress: Progress | None = None
) -> dict[str, Any]:
    """Idle round-trip time over HTTP, server processing time removed via Server-Timing.

    A ping without an answer is lost. One the server answers with an error, such as 401
    for want of an access code, ends the measurement with it (httpx.HTTPStatusError), as a
    refused download does: a refusal isn't a lost ping."""
    base = _base(target)
    samples: list[float] = []
    lost = 0
    async with _client(timeout=request_timeout) as client:
        (await client.get(f"{base}{API}/ping")).raise_for_status()  # open + warm the connection
        for i in range(count):
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{base}{API}/ping", params={"r": i})
            except httpx.TransportError:
                lost += 1
                continue
            r.raise_for_status()
            rtt = (time.perf_counter() - t0) * 1000 - server_timing_ms(
                r.headers.get("server-timing")
            )
            samples.append(max(rtt, 0.0))
            await _emit(progress, "latency", {"sample": rtt, "index": i})
    return summarize(samples, lost)


async def new_connection(base: str, limit: float = 5.0) -> dict[str, float] | None:
    """Time a new connection's steps, in ms: the TCP connect, the TLS handshake (https
    only) and a first request, as the IETF's responsiveness draft does. None if one failed."""
    url = urlsplit(base)
    tls = url.scheme == "https"
    host = url.hostname or ""
    path = f"{url.path.rstrip('/')}{API}/ping?{urlencode(_params())}"
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {url.netloc}\r\nUser-Agent: {USER_AGENT}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    try:
        async with asyncio.timeout(limit):
            t0 = time.perf_counter()
            reader, writer = await asyncio.open_connection(host, url.port or (443 if tls else 80))
            t1 = time.perf_counter()
            try:
                if tls:
                    await writer.start_tls(ssl.create_default_context(), server_hostname=host)
                t2 = time.perf_counter()
                writer.write(request)
                await writer.drain()
                status = await reader.readline()
                t3 = time.perf_counter()
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
    except (OSError, TimeoutError):
        return None
    if not status.startswith(b"HTTP/"):
        return None
    steps = {"tcp": (t1 - t0) * 1000, "http": (t3 - t2) * 1000}
    return steps | {"tls": (t2 - t1) * 1000} if tls else steps


class _NewConnections:
    """New connections opened while a transfer runs, one a second: the responsiveness
    test's probes away from the load."""

    def __init__(self, base: str, every: float = 1.0) -> None:
        self.base = base
        self.every = every
        self.found: list[dict[str, float]] = []
        self.lost = 0

    async def run(self, stop: asyncio.Event) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 0.5)
        while not stop.is_set():
            started = time.perf_counter()
            steps = await new_connection(self.base)
            if stop.is_set():
                break
            if steps:
                self.found.append(steps)
            else:
                self.lost += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), max(0, self.every - (time.perf_counter() - started))
                )

    def summary(self) -> dict[str, Any]:
        tls = urlsplit(self.base).scheme == "https"
        out: dict[str, Any] = {
            "setup_ms": [round(sum(s.values()), 3) for s in self.found],
            "tcp_ms": [round(s["tcp"], 3) for s in self.found],
            "http_ms": [round(s["http"], 3) for s in self.found],
            "lost": self.lost,
            "tls": tls,
        }
        if tls:
            out["tls_ms"] = [round(s["tls"], 3) for s in self.found]
        return out


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
    transfer: str = "",
) -> dict[str, Any]:
    base = _base(target)
    per_stream = [0] * streams
    stop = asyncio.Event()
    prober = _WsProber(base) if loaded_latency else None
    connections = _NewConnections(base) if loaded_latency else None
    test = TEST.get() or secrets.token_hex(8)

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
                params = {"bytes": cap, "test": test} | ({"transfer": transfer} if transfer else {})
                async with client.stream("GET", url, params=params) as r:
                    if r.is_error:  # such as 503 from a traffic limit: not test data
                        await r.aread()
                        r.raise_for_status()
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
                yield payload.span(sent, n)
                # The previous chunk has been accepted by the transport once we resume.
                bins.add(n, time.perf_counter())
                per_stream[i] += n
                sent += n

        async def upload(i: int) -> None:
            while time.perf_counter() < deadline:
                r = await client.post(f"{base}{API}/up", params={"test": test}, content=body(i))
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
        if connections:
            side.append(asyncio.create_task(connections.run(stop)))
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
    if connections:
        out["connections"] = connections.summary()
    return out


async def measure_download(
    target: str,
    seconds: float = 10.0,
    streams: int = 4,
    loaded_latency: bool = True,
    progress: Progress | None = None,
    transfer: str = "",
) -> dict[str, Any]:
    """``transfer="one_stream"`` names the one stream's download after a test's download,
    which the server times apart (GET /api/v1/tests/{id})."""
    return await _throughput(
        "download", target, seconds, streams, loaded_latency, progress, transfer
    )


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
    size: int = PROBE_BYTES,
    wait_ms: float = 2000.0,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Send numbered UDP probes over an unreliable data channel and count the echoes. A
    server that stamps its echoes also gives each direction's delays and losses."""
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    base = _base(target)
    size = max(size, PROBE.size)
    sent_at: dict[int, float] = {}
    rtts: list[float | None] = [None] * packets
    server: list[float | None] = [None] * packets
    back: list[float | None] = [None] * packets
    counts: list[int | None] = [None] * packets
    stamped = False
    order: list[int] = []
    duplicates = 0
    loop = asyncio.get_running_loop()
    opened = asyncio.Event()
    control_opened = asyncio.Event()
    all_back = asyncio.Event()
    control_reply: asyncio.Future[dict[str, Any]] = loop.create_future()
    stamp_reply: asyncio.Future[dict[str, Any]] = loop.create_future()

    async with _client() as client:
        info = (await client.get(f"{base}{API}/info")).json()
    stamps = size >= PROBE.size + STAMP.size and info.get("capabilities", {}).get("stamps")
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
        now = time.perf_counter() * 1000
        seq, _ = PROBE.unpack_from(message)
        if seq >= packets or seq not in sent_at:
            return
        if rtts[seq] is not None:
            duplicates += 1
            return
        rtts[seq] = now - sent_at[seq]
        back[seq] = now
        if stamped and len(message) >= PROBE.size + STAMP.size:
            server[seq], counts[seq] = STAMP.unpack_from(message, PROBE.size)
        order.append(seq)
        if len(order) == packets:
            all_back.set()

    @control.on("open")
    def _on_control_open() -> None:
        control_opened.set()

    @control.on("message")
    def _on_control(message: bytes | str) -> None:
        with contextlib.suppress(ValueError):
            reply = json.loads(message)
            if reply.get("op") == "stats" and not control_reply.done():
                control_reply.set_result(reply)
            elif reply.get("op") == "stamp" and not stamp_reply.done():
                stamp_reply.set_result(reply)

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
        if stamps:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(control_opened.wait(), timeout=2)
                control.send(json.dumps({"op": "stamp"}))
                stamped = bool(await asyncio.wait_for(stamp_reply, timeout=2))

        start = time.perf_counter()
        for seq in range(packets):
            # Pace against the schedule rather than sleeping a fixed interval, so
            # event-loop overhead does not stretch the test.
            delay = start + seq * interval_ms / 1000 - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.perf_counter() * 1000
            sent_at[seq] = now
            probe.send(PROBE.pack(seq, now) + os.urandom(size - PROBE.size))  # random padding
            if progress and seq % 25 == 0:
                await _emit(progress, "loss", {"sent": seq + 1})

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(all_back.wait(), wait_ms / 1000)

        control.send(json.dumps({"op": "stats"}))
        try:
            server_counts = await asyncio.wait_for(control_reply, timeout=3)
        except TimeoutError:
            server_counts = None
    finally:
        await pc.close()

    result = loss_summary(rtts, order, duplicates, server_counts, "webrtc", interval_ms, size)
    if stamped:
        sent = [sent_at.get(i, 0.0) for i in range(packets)]
        total = server_counts["rx"] if server_counts else None
        if ways := quality.one_way(sent, server, back, counts, total):
            result |= ways
    return result


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
    one_stream: bool | None = None,
) -> dict[str, Any]:
    """A full test toward ``target``. With ``local``, the result also gives this host's
    own address on the connection the server saw, as the socket was bound, and the MAC
    of the NIC the kernel routes it out of (the CLI's; a server measuring for an API
    caller keeps them to itself). With ``one_stream``, one stream's download follows the
    download, unless the test already uses one; None follows the server's default."""
    base = _base(target)
    started = datetime.now(UTC)
    async with _client() as client:
        info = (await client.get(f"{base}{API}/info")).json()
        r = await client.get(f"{base}{API}/whoami")
        whoami = r.json()
        stream = r.extensions.get("network_stream") if local else None
        mine = stream.get_extra_info("client_addr") if stream else None
        theirs = stream.get_extra_info("server_addr") if stream else None
    if one_stream is None:
        one_stream = bool(info.get("defaults", {}).get("one_stream", True))
    one_stream = one_stream and streams > 1

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
            "one_stream": one_stream,
        },
    }
    if mine:
        result["client"]["local_ip"] = mine[0]
        if mac := await asyncio.to_thread(iface.mac_of, mine[0], theirs[0] if theirs else None):
            result["client"] |= {"mac": mac, "mac_source": "nic"}
    result["latency"] = await measure_latency(base, progress=progress)
    if steps := await new_connection(base):  # what a new connection costs, idle
        result["latency"]["setup_ms"] = round(sum(steps.values()), 3)
    test_id = secrets.token_hex(8)
    test = TEST.set(test_id)  # one test: the upload goes on if the download did
    try:
        result["download"] = await measure_download(base, seconds, streams, progress=progress)
        if one_stream:  # what a single file copy gets

            async def one(_: str, data: dict[str, Any]) -> None:
                await _emit(progress, "1 stream", data)

            result["one_stream"] = await measure_download(
                base,
                seconds,
                1,
                loaded_latency=False,
                progress=one if progress else None,
                transfer="one_stream",
            )
        result["upload"] = await measure_upload(base, seconds, streams, progress=progress)
    finally:
        TEST.reset(test)
    if info.get("capabilities", {}).get("test_cpu"):
        await server_cpu(base, test_id, result)
    if loss and info.get("capabilities", {}).get("webrtc"):
        try:
            result["loss"] = await measure_loss(base, packets, interval_ms, progress=progress)
        except Exception as exc:
            result["loss"] = {"error": f"{type(exc).__name__}: {exc}"}
    result["bufferbloat"] = bufferbloat(result)
    if call := quality.call_quality(result.get("loss")):
        result["call"] = call
    if rpm := quality.responsiveness(result):
        result["responsiveness"] = rpm
    result["duration_s"] = (datetime.now(UTC) - started).total_seconds()
    return result


async def server_cpu(base: str, test: str, result: dict[str, Any]) -> None:
    """Each transfer's ``server_cpu``: the busiest share of a core among the server's
    workers that served it. At 0.9 or more, the server's CPU set its rate (docs/cpu.md).
    Left out where the server can't say."""
    try:
        async with _client() as client:
            r = await client.get(f"{base}{API}/tests/{test}")
            r.raise_for_status()
            parts = r.json()
    except (httpx.HTTPError, ValueError):
        return
    for name in ("download", "one_stream", "upload"):
        cpu = (parts.get(name) or {}).get("cpu")
        if name in result and isinstance(cpu, int | float):
            result[name]["server_cpu"] = cpu


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
