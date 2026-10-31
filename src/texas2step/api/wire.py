"""Wire endpoints: the primitives every measurement is built from.

They are deliberately tiny and stable so that browsers, the CLI and plain ``curl``
can all drive them. Every response carries ``Server-Timing`` so clients can remove
server processing time from their round-trip measurements.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from .. import payload
from ..cpu import CpuMonitor, Watched
from ..rtc import RtcBusy, RtcUnavailable
from ..traffic import Closed, TrafficMeter
from .access import WS_PROTOCOL, pass_issued
from .meta import client_address

router = APIRouter(prefix="/api/v1", tags=["wire"])

# The echo socket's close code at session_max_runtime: the page stops rather than open
# another (docs/api.md). 4000-4999 are for applications to define.
RUNTIME_CLOSE = 4001

NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}


def _timing(**metrics: float) -> str:
    return ", ".join(f"{name};dur={ms:.3f}" for name, ms in metrics.items())


@router.get("/ping", summary="Minimal round-trip probe")
async def ping() -> Response:
    """Returns ``{"pong": true}``. Use with keep-alive and time request→first byte."""
    t0 = time.perf_counter()
    return Response(
        b'{"pong":true}',
        media_type="application/json",
        headers={**NO_STORE, "Server-Timing": _timing(app=(time.perf_counter() - t0) * 1000)},
    )


TestId = Annotated[
    str | None,
    Query(
        alias="test",
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="The test this request belongs to: once a test is let in, a traffic "
        "limit reached while it runs doesn't stop it (docs/traffic.md).",
    ),
]


async def _admit(request: Request, test: str | None) -> tuple[TrafficMeter, str] | None:
    """The meter and whose traffic this is, or 503 if a traffic limit refuses the test, or
    other work on the host does (cpu_busy_percent)."""
    meter: TrafficMeter | None = request.app.state.traffic
    kind = meter.kind(client_address(request)[0]) if meter is not None else None
    if meter is not None and kind is not None and await asyncio.to_thread(meter.admit, test, kind):
        raise Closed(meter.notice())
    monitor: CpuMonitor = request.app.state.cpu
    if monitor.busy_share and not await asyncio.to_thread(monitor.admit, test):
        raise Closed(monitor.notice())
    return (meter, kind) if meter is not None and kind is not None else None


async def _watched(
    body: AsyncIterator[bytes], monitor: CpuMonitor, test: str | None, part: str, tasks: set
) -> AsyncIterator[bytes]:
    """Counts what a download sends, for the CPU figures, and times this worker's CPU
    while it does, for GET /api/v1/tests/{id}."""
    watch = Watched(monitor, test, part)
    try:
        async for chunk in body:
            yield chunk
            monitor.work.sent += len(chunk)
            watch.moved()
    finally:
        watch.done(tasks)


async def _metered(
    body: AsyncIterator[bytes], meter: TrafficMeter, kind: str
) -> AsyncIterator[bytes]:
    """Counts each chunk once the server has taken it, and ends with the client.

    Once the client is gone, uvicorn drops what is sent without pausing, and the payload
    never pauses either, so nothing would let Starlette's disconnect watch end the
    response: the rest of it would be counted, and never sent. Pausing here lets it.
    """
    async for chunk in body:
        yield chunk
        await asyncio.sleep(0)
        meter.add(kind, sent=len(chunk))


@router.get("/down", summary="Stream founding documents, then random bytes (download test)")
async def down(
    request: Request,
    size: Annotated[
        int | None, Query(alias="bytes", ge=0, description="Exact number of bytes to send.")
    ] = None,
    seconds: Annotated[
        float | None,
        Query(gt=0, description="Stream for this long instead (chunked, not byte-capped)."),
    ] = None,
    test: TestId = None,
    transfer: Annotated[
        Literal["download", "one_stream"],
        Query(
            description="Which of the test's transfers this is: its download, or the one "
            "stream's after it, for GET /api/v1/tests/{id}."
        ),
    ] = "download",
) -> Response:
    """``curl -o /dev/null 'https://host/api/v1/down?bytes=1000000000'`` or ``?seconds=10``.

    The body starts with five founding documents as UTF-8 text, about 770 KB, then random
    bytes to the end (``download_documents``; docs/methodology.md#what-the-tests-send).
    """
    t0 = time.perf_counter()
    settings = request.app.state.settings
    metered = await _admit(request, test)
    headers = {**NO_STORE, "Content-Type": "application/octet-stream"}
    text = payload.TEXT if settings.download_documents else b""
    if seconds is not None:
        body = payload.stream_for(min(seconds, settings.max_stream_seconds), text)
    else:
        n = min(size if size is not None else 100_000_000, settings.max_download_bytes)
        if n == 0:
            headers["Server-Timing"] = _timing(app=(time.perf_counter() - t0) * 1000)
            return Response(b"", headers=headers)
        body = payload.stream_bytes(n, text)
        headers["Content-Length"] = str(n)
    if metered:
        body = _metered(body, *metered)
    body = _watched(body, request.app.state.cpu, test, transfer, request.app.state.tasks)
    headers["Server-Timing"] = _timing(app=(time.perf_counter() - t0) * 1000)
    return StreamingResponse(body, headers=headers)


@router.post("/up", summary="Receive and discard a request body (upload test)")
async def up(request: Request, test: TestId = None) -> Response:
    """Reads the body as it arrives (no buffering, no disk) and reports what it saw.

    ``rx_ms`` spans first to last body chunk, so ``mbps`` is the server-observed
    upload rate excluding request setup.
    """
    limit = request.app.state.settings.max_upload_bytes
    metered = await _admit(request, test)
    monitor: CpuMonitor = request.app.state.cpu
    watch = Watched(monitor, test, "upload")
    total = first_len = 0
    first = last = None
    try:
        async for data in request.stream():
            if not data:
                continue
            now = time.perf_counter()
            if first is None:
                first, first_len = now, len(data)
            last = now
            total += len(data)
            monitor.work.received += len(data)
            watch.moved()
            if metered:
                metered[0].add(metered[1], received=len(data))
            if total > limit:
                raise HTTPException(413, f"upload exceeds {limit} bytes")
    except ClientDisconnect:
        # Browsers cancel in-flight uploads when a timed test ends. Nobody is left to
        # answer, so this is never sent; 499 is nginx's code for "client closed request".
        return Response(status_code=499)
    t_done = time.perf_counter()
    await watch.save()  # before the answer, which the client asks about it after
    rx_ms = (last - first) * 1000 if first is not None and last is not None else 0.0
    mbps = (total - first_len) * 8 / (rx_ms * 1000) if rx_ms > 0 else None
    return JSONResponse(
        {"bytes": total, "rx_ms": round(rx_ms, 3), "mbps": mbps},
        headers={
            **NO_STORE,
            "Server-Timing": _timing(app=(time.perf_counter() - t_done) * 1000, rx=rx_ms),
        },
    )


@router.websocket("/ws")
async def ws_echo(ws: WebSocket) -> None:
    """Echoes every message unchanged. Used for latency under load and live monitoring."""
    # The page's own sockets carry the pass in a subprotocol (api/access.py): answer it.
    offered = WS_PROTOCOL in ws.scope.get("subprotocols", [])
    await ws.accept(subprotocol=WS_PROTOCOL if offered else None)
    # With access codes, a block set by hand on the client's network ends its socket too,
    # as it ends its pass (app.AccessGate), and so does ending the passes issued before
    # some time, its own among them; looked at as messages come, every few seconds.
    ip, _ = client_address(ws)
    blocks = ws.app.state.blocks if ws.app.state.codes is not None else None
    issued = await pass_issued(ws)
    checked = opened = time.monotonic()
    limit = ws.app.state.settings.session_max_runtime
    work = ws.app.state.cpu.work
    try:
        while True:
            message = await ws.receive()
            if blocks is not None and time.monotonic() - checked >= blocks.EVERY:
                checked = time.monotonic()
                ended = issued is not None and issued <= await blocks.ended()
                if ended or await blocks.cover(ip):
                    await ws.close(code=1008, reason="access code required")
                    return
            if message["type"] == "websocket.disconnect":
                return
            if limit and time.monotonic() - opened > limit:
                await ws.close(code=RUNTIME_CLOSE, reason="session_max_runtime")
                return
            if message.get("bytes") is not None:
                await ws.send_bytes(message["bytes"])
                work.sent += len(message["bytes"])
                work.received += len(message["bytes"])
            elif message.get("text") is not None:
                await ws.send_text(message["text"])
                work.sent += len(message["text"])
                work.received += len(message["text"])
    except WebSocketDisconnect:
        return


class Offer(BaseModel):
    sdp: str = Field(max_length=64_000)
    type: Literal["offer"] = "offer"


class Answer(BaseModel):
    id: str
    sdp: str
    type: Literal["answer"]


@router.post("/rtc/offer", response_model=Answer, summary="WebRTC signalling for the UDP probe")
async def rtc_offer(offer: Offer, request: Request) -> dict[str, str]:
    """Answer an SDP offer carrying a ``probe`` channel (unordered, ``maxRetransmits: 0``)
    and an optional reliable ``control`` channel. Probes are echoed verbatim; on
    ``control`` send ``{"op":"stats"}`` to read the server's receive/transmit counters.
    """
    if "m=application" not in offer.sdp:
        raise HTTPException(400, "offer has no data channel (m=application)")
    try:
        # Whose traffic a voice or video profile is, should a traffic limit count it.
        meter = request.app.state.traffic
        ip, _ = client_address(request)
        whose = meter.kind(ip) if meter is not None else None
        # With access codes, the session ends should a block set by hand cover this client,
        # or the passes issued before its own be ended.
        watched = ip if request.app.state.codes is not None else None
        issued = await pass_issued(request)
        client = request.app.state.clients.key(request, ip)[0]  # whose page or run it is
        rtc = request.app.state.rtc
        return await rtc.answer(offer.sdp, whose=whose, ip=watched, issued=issued, client=client)
    except RtcUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    except RtcBusy as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, f"bad offer: {exc}") from exc
