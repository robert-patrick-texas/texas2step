"""Result history: browsers (and ``texas2step run --save``) post finished tests here."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .meta import client_address

router = APIRouter(prefix="/api/v1/results", tags=["results"])

MAX_RESULT_BYTES = 2 * 1024 * 1024


def _store(request: Request) -> Any:
    store = request.app.state.store
    if store is None:
        raise HTTPException(404, "result storage is disabled on this server")
    return store


@router.post("", status_code=201, summary="Save a completed test result")
async def save(request: Request) -> dict[str, Any]:
    store = _store(request)
    raw = await request.body()
    if len(raw) > MAX_RESULT_BYTES:
        raise HTTPException(413, "result too large")
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "body must be JSON") from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise HTTPException(422, "expected a texas2step result object (version 1)")
    ip, _ = client_address(request)
    return await run_in_threadpool(store.add, result, ip)


@router.get("", summary="Recent results (summaries)")
async def recent(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    before: float | None = Query(None, description="Only results saved before this epoch time"),
) -> dict[str, Any]:
    store = _store(request)
    return {"results": await run_in_threadpool(store.recent, limit, before)}


@router.get("/{rid}", summary="A saved result, in full")
async def one(rid: str, request: Request) -> dict[str, Any]:
    result = await run_in_threadpool(_store(request).get, rid)
    if result is None:
        raise HTTPException(404, "no such result")
    return result
