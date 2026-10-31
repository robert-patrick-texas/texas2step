"""The server's CPU, for the service desk and monitoring, and what a test's client may ask
of its own test: whether the server's CPU held a transfer back (docs/cpu.md)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse

from ..cpu import KEEP_DAYS, CpuMonitor

router = APIRouter(prefix="/api/v1", tags=["meta"])


def _desk(request: Request) -> CpuMonitor:
    if not request.app.state.settings.internal_networks:
        raise HTTPException(404, "list internal_networks to see the server's CPU over the API")
    return request.app.state.cpu


@router.get("/cpu", summary="The server's CPU and what it serves (internal only)")
async def figures(
    request: Request,
    days: Annotated[int, Query(ge=0, le=KEEP_DAYS, description="Days of daily figures.")] = 30,
) -> dict[str, Any]:
    """The workers' CPU now (the last 30 s), nginx's, the host's and other work's, with
    the UDP sessions and probes and the tests' traffic they served; the last hour by the
    minute; and each day's figures, the latest first. ``busy`` says whether other work
    keeps tests out (``cpu_busy_percent``), or is null without it. What the server's
    ``cpu --json`` command prints (docs/cpu.md).

    Only for ``internal_networks``: a server without them answers 404.
    """
    monitor = _desk(request)
    try:
        return await asyncio.to_thread(monitor.report, time.time(), monitor.busy_state(), days)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, f"can't read {monitor.path}: {exc}") from exc


@router.get(
    "/metrics",
    summary="The CPU figures for Prometheus (internal only)",
    response_class=PlainTextResponse,
)
async def metrics(request: Request) -> PlainTextResponse:
    """Each worker's CPU time, memory, test bytes and UDP probes as counters, and the
    host's, nginx's and other work's share of its CPU over the last 30 s, in Prometheus's
    text format. Scrape it from ``internal_networks``: a server without them answers 404.
    """
    monitor = _desk(request)
    try:
        text = await asyncio.to_thread(monitor.metrics, time.time(), monitor.busy_state())
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, f"can't read {monitor.path}: {exc}") from exc
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4")


@router.get("/tests/{test}", summary="Whether the server's CPU held a test's transfers back")
async def test(
    request: Request,
    test: Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
) -> dict[str, Any]:
    """For each of a test's transfers (``download``, ``one_stream``, ``upload``) that
    this server served for a second or more, by the ``?test=`` its requests carried: the
    highest share of a core among the worker processes that served it, and ``bound``,
    whether that was 90 % or more, so the server's CPU, not the network, set its rate.
    Kept an hour; a transfer the server has no figure for is left out.
    """
    monitor: CpuMonitor = request.app.state.cpu
    try:
        parts = await asyncio.to_thread(monitor.test, test)
    except (OSError, sqlite3.Error):
        parts = {}
    return {"test": test, **parts}
