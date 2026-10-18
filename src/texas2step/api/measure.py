"""Discrete measurement functions executed *by this server* against another endpoint.

``POST /api/v1/measure/download {"target": "https://peer"}`` makes this server run
the download test toward ``peer`` and return the result, so any two texas2step
servers can be measured from a single API call. Targets must be configured
locations unless ``allow_any_target`` is set (prevents use as an SSRF relay).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import engine

router = APIRouter(prefix="/api/v1/measure", tags=["measure"])

_slots = asyncio.Semaphore(2)


class Target(BaseModel):
    target: str = Field(description="Base URL of a texas2step server, e.g. https://peer.example")


class LatencyRequest(Target):
    count: int = Field(20, ge=1, le=1000)


class ThroughputRequest(Target):
    seconds: float = Field(10.0, gt=0, le=60)
    streams: int = Field(4, ge=1, le=32)
    loaded_latency: bool = True


class LossRequest(Target):
    packets: int = Field(500, ge=1, le=10_000)
    interval_ms: float = Field(10.0, ge=1, le=1000)
    size: int = Field(64, ge=12, le=1200)


class FullRequest(Target):
    seconds: float = Field(10.0, gt=0, le=60)
    streams: int = Field(4, ge=1, le=32)
    packets: int = Field(500, ge=0, le=10_000)
    interval_ms: float = Field(10.0, ge=1, le=1000)
    loss: bool = True


async def _run(
    request: Request, target: str, job: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.measure_enabled:
        raise HTTPException(404, "server-side measurement is disabled")
    if not settings.is_allowed_target(target):
        raise HTTPException(403, "target is not a configured location")
    if _slots.locked():
        raise HTTPException(429, "measurement already in progress, retry shortly")
    async with _slots:
        try:
            return await job()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"target unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise HTTPException(504, "target timed out") from exc


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
    )


@router.post("/upload", summary="Upload throughput to a target")
async def upload(body: ThroughputRequest, request: Request) -> dict[str, Any]:
    return await _run(
        request,
        body.target,
        lambda: engine.measure_upload(body.target, body.seconds, body.streams, body.loaded_latency),
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
        ),
    )
