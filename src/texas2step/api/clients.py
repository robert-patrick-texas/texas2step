"""The pages and command-line runs using this server now, for the service desk."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import clients

router = APIRouter(prefix="/api/v1", tags=["meta"])


@router.get("/clients", summary="The pages and command-line runs active now (internal only)")
async def active(request: Request) -> dict[str, Any]:
    """Each page and command-line run that sent a request in the last minute, by the
    session id it sends, with its page, program, addresses, what it is doing now and the
    bytes it moved; and in ``ended``, those that ended in the last hour, the latest first.
    What the server's ``clients --json`` command prints (docs/clients.md).

    Only for ``internal_networks``: a server without them answers 404.
    """
    settings = request.app.state.settings
    if not settings.internal_networks:
        raise HTTPException(404, "list internal_networks to see the clients over the API")
    now = time.time()
    try:
        board = await asyncio.to_thread(clients.read, settings.clients_file, now)
    except FileNotFoundError:
        board = clients.Board([], [], full=False)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, f"can't read {settings.clients_file}: {exc}") from exc
    return board.public(now, settings.clients_max)
