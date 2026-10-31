"""Discrete measurement functions executed *by this server* against another endpoint.

``POST /api/v1/measure/download {"target": "https://peer"}`` makes this server run
the download test toward ``peer`` and return the result, so any two texas2step
servers can be measured from a single API call. Targets must be configured
locations unless ``allow_any_target`` is set (prevents use as an SSRF relay).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import engine
from ..store import TRANSFERS
from ..traffic import INSIDE, LOCAL, OUTSIDE, Closed, TrafficMeter

router = APIRouter(prefix="/api/v1/measure", tags=["measure"])

_slots = asyncio.Semaphore(2)


class Target(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    target: str = Field(
        description="Base URL of the server to measure toward, e.g. https://peer.example: "
        "one in [[locations]], unless allow_any_target is set"
    )


class LatencyRequest(Target):
    count: int = Field(20, ge=1, le=1000)


class ThroughputRequest(Target):
    seconds: float = Field(10.0, gt=0, le=60)
    streams: int = Field(4, ge=1, le=32)
    loaded_latency: bool = True


class LossRequest(Target):
    packets: int = Field(500, ge=1, le=10_000)
    interval_ms: float = Field(10.0, ge=1, le=1000)
    size: int = Field(engine.PROBE_BYTES, ge=12, le=1200)


class FullRequest(Target):
    seconds: float = Field(10.0, gt=0, le=60)
    streams: int = Field(4, ge=1, le=32)
    packets: int = Field(500, ge=0, le=10_000)
    interval_ms: float = Field(10.0, ge=1, le=1000)
    loss: bool = True
    one_stream: bool | None = Field(None, description="Default: as the target server says")


async def _run(
    request: Request,
    target: str,
    job: Callable[[], Awaitable[dict[str, Any]]],
    *,
    moves: bool = False,  # a throughput test, which a traffic limit may refuse
) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.measure_enabled:
        raise HTTPException(404, "server-side measurement is disabled")
    if not settings.is_allowed_target(target):
        raise HTTPException(403, "target is not a configured location")
    meter: TrafficMeter | None = request.app.state.traffic
    kind = await _whose(target, meter) if meter is not None and moves else None
    if meter is not None and kind is not None and await asyncio.to_thread(meter.admit, None, kind):
        raise Closed(meter.notice())
    monitor = request.app.state.cpu
    if moves and monitor.busy_share and not await asyncio.to_thread(monitor.admit, None):
        raise Closed(monitor.notice())  # other work on this host (cpu_busy_percent)
    if _slots.locked():
        raise HTTPException(429, "measurement already in progress, retry shortly")
    async with _slots:
        # The target lists this test as a server's, apart from any other (docs/clients.md).
        engine.PAGE.set("server")
        engine.SESSION.set(engine.new_session())
        engine.PASS.set(await _pass(request, target))
        try:
            result = await job()
        except httpx.HTTPStatusError as exc:
            answer = f"{exc.response.status_code} {_detail(exc.response)}"
            raise HTTPException(502, f"target answered {answer}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"target unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise HTTPException(504, "target timed out") from exc
    if meter is not None and kind is not None:
        _count(meter, kind, result)
    return result


async def _pass(request: Request, target: str) -> str:
    """A pass for this server's test toward another in [[locations]], issued with the
    access key: a server with the same key lets the test in as it would a visitor who
    entered its code, and grants nothing more, so this server needn't be in its
    internal_networks. Only to [[locations]], never to a target allow_any_target alone lets
    in: a pass works on every server with the key, for access_pass_hours. "" without a key,
    and a server with another key answers 401 as before."""
    issuer = request.app.state.issuer
    if issuer is None or not request.app.state.settings.is_location(target):
        return ""
    return issuer.issue(ended=await request.app.state.blocks.ended())[0]


async def _whose(target: str, meter: TrafficMeter) -> str:
    """Whose traffic a test toward ``target`` is: inside if every address it has is."""
    host = httpx.URL(engine._base(target)).host
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return OUTSIDE
    kinds = {meter.kind(str(info[4][0])) for info in found}
    if kinds == {LOCAL}:
        return LOCAL
    return INSIDE if kinds and kinds <= {INSIDE, LOCAL} else OUTSIDE


def _count(meter: TrafficMeter, kind: str, result: dict[str, Any]) -> None:
    """This server's side of a test it ran: what it downloaded, it received."""
    parts = [result] if "direction" in result else [result.get(d) or {} for d in TRANSFERS]
    for part in parts:
        moved = part.get("bytes")
        if not isinstance(moved, int | float):
            continue
        if part.get("direction") == "download":
            meter.add(kind, received=int(moved))
        elif part.get("direction") == "upload":
            meter.add(kind, sent=int(moved))


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail") or response.reason_phrase)
    except (ValueError, AttributeError, httpx.ResponseNotRead):
        return response.reason_phrase


@router.post("/latency", summary="Idle HTTP latency and jitter toward a target")
async def latency(body: LatencyRequest, request: Request) -> dict[str, Any]:
    return await _run(request, body.target, lambda: engine.measure_latency(body.target, body.count))


@router.post("/download", summary="Download throughput from a target")
async def download(body: ThroughputRequest, request: Request) -> dict[str, Any]:
    return await _run(
        request,
        body.target,
        lambda: engine.measure_download(
            body.target, body.seconds, body.streams, body.loaded_latency
        ),
        moves=True,
    )


@router.post("/upload", summary="Upload throughput to a target")
async def upload(body: ThroughputRequest, request: Request) -> dict[str, Any]:
    return await _run(
        request,
        body.target,
        lambda: engine.measure_upload(body.target, body.seconds, body.streams, body.loaded_latency),
        moves=True,
    )


@router.post("/loss", summary="UDP packet loss, RTT and jitter toward a target (WebRTC)")
async def loss(body: LossRequest, request: Request) -> dict[str, Any]:
    return await _run(
        request,
        body.target,
        lambda: engine.measure_loss(body.target, body.packets, body.interval_ms, body.size),
    )


@router.post("/full", summary="Latency, download, upload and loss toward a target")
async def full(body: FullRequest, request: Request) -> dict[str, Any]:
    return await _run(
        request,
        body.target,
        lambda: engine.run_all(
            body.target,
            body.seconds,
            body.streams,
            body.packets,
            body.interval_ms,
            loss=body.loss and body.packets > 0,
            one_stream=body.one_stream,
        ),
        moves=True,
    )
