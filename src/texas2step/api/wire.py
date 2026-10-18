"""Wire endpoints: the primitives every measurement is built from.

They are deliberately tiny and stable so that browsers, the CLI and plain ``curl``
can all drive them. Every response carries ``Server-Timing`` so clients can remove
server processing time from their round-trip measurements.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from .. import payload
from ..rtc import RtcBusy, RtcUnavailable

router = APIRouter(prefix="/api/v1", tags=["wire"])

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


@router.get("/down", summary="Stream incompressible bytes (download test)")
async def down(
    request: Request,
    size: Annotated[
        int | None, Query(alias="bytes", ge=0, description="Exact number of bytes to send.")
    ] = None,
    seconds: Annotated[
        float | None,
        Query(gt=0, description="Stream for this long instead (chunked, not byte-capped)."),
    ] = None,
) -> Response:
    """``curl -o /dev/null 'https://host/api/v1/down?bytes=1000000000'`` or ``?seconds=10``."""
    t0 = time.perf_counter()
    settings = request.app.state.settings
    headers = {**NO_STORE, "Content-Type": "application/octet-stream"}
    if seconds is not None:
        body = payload.stream_for(min(seconds, settings.max_stream_seconds))
    else:
        n = min(size if size is not None else 100_000_000, settings.max_download_bytes)
        if n == 0:
            headers["Server-Timing"] = _timing(app=(time.perf_counter() - t0) * 1000)
            return Response(b"", headers=headers)
        body = payload.stream_bytes(n)
        headers["Content-Length"] = str(n)
    headers["Server-Timing"] = _timing(app=(time.perf_counter() - t0) * 1000)
    return StreamingResponse(body, headers=headers)


@router.post("/up", summary="Receive and discard a request body (upload test)")
async def up(request: Request) -> Response:
    """Reads the body as it arrives (no buffering, no disk) and reports what it saw.

    ``rx_ms`` spans first to last body chunk, so ``mbps`` is the server-observed
    upload rate excluding request setup.
    """
    limit = request.app.state.settings.max_upload_bytes
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
            if total > limit:
                raise HTTPException(413, f"upload exceeds {limit} bytes")
    except ClientDisconnect:
        # Browsers cancel in-flight uploads when a timed test ends. Nobody is left to
        # answer, so this is never sent; 499 is nginx's code for "client closed request".
        return Response(status_code=499)
    t_done = time.perf_counter()
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
    await ws.accept()
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await ws.send_bytes(message["bytes"])
            elif message.get("text") is not None:
                await ws.send_text(message["text"])
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
        return await request.app.state.rtc.answer(offer.sdp)
    except RtcUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    except RtcBusy as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, f"bad offer: {exc}") from exc
