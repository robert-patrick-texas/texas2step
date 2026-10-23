"""Access codes (docs/access.md): whether a client needs one, and a pass for a right one."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..access import is_internal
from .meta import client_address

router = APIRouter(prefix="/api/v1/access", tags=["access"])
log = logging.getLogger(__name__)


class CodeIn(BaseModel):
    code: str = Field(max_length=200, description="The code, with any case and separators")


def internal(request: Request) -> bool:
    ip, _ = client_address(request)
    return is_internal(ip, request.app.state.settings.internal_networks)


def desk(request: Request) -> bool:
    """Whether this client gets saved results, the API explorer and measurements."""
    return not request.app.state.settings.internal_networks or internal(request)


@router.get("", summary="Whether this client needs an access code")
async def status(
    request: Request,
    pass_: str = Query("", alias="pass", description="A pass to check, from POST /access"),
) -> dict[str, Any]:
    """``required``: tests need a pass from this client. ``desk``: this client gets the
    service desk's functions (saved results, the API explorer, measurements)."""
    codes = request.app.state.codes
    body: dict[str, Any] = {
        "required": codes is not None and not internal(request),
        "desk": desk(request),
    }
    if codes is not None and pass_:
        expires = codes.pass_expires(pass_)
        body["pass"] = {"valid": expires is not None, "expires": expires}
    return body


@router.post("", summary="Exchange the access code for a pass")
async def unlock(request: Request, given: CodeIn) -> dict[str, Any]:
    """A pass for the current code or a standing phrase, valid on every server that holds
    the same key. Send it as ``?pass=`` with each request. Wrong codes are counted per
    client address."""
    codes = request.app.state.codes
    if codes is None:
        raise HTTPException(404, "this server doesn't ask for an access code")
    ip, _ = client_address(request)
    attempts = request.app.state.attempts
    if attempts.blocked(ip):
        raise HTTPException(429, "too many wrong codes; try again in a few minutes")
    now = datetime.now(UTC)
    if not codes.check(given.code, now):
        # A standing phrase: every use is logged by its name, so a leaked one shows.
        phrase = codes.phrase(given.code)
        if phrase is None or codes.expired(phrase, now):
            attempts.failed(ip)
            if phrase is None:
                log.warning("wrong access code from %s", ip)
            else:
                log.warning("expired access phrase %s from %s", phrase.name, ip)
            raise HTTPException(403, "that isn't the current access code")
        log.warning("access phrase %s used from %s", phrase.name, ip)
    token, expires = codes.issue()
    return {"pass": token, "expires": expires}


@router.get("/code", summary="The current access code, for the service desk")
async def current(request: Request) -> dict[str, Any]:
    """Only for clients in ``internal_networks``, on a server that has them."""
    codes = request.app.state.codes
    if codes is None or not request.app.state.settings.internal_networks or not internal(request):
        raise HTTPException(404, "Not Found")
    now = datetime.now(UTC)
    code = codes.current(now)
    return {
        "code": code.code,
        "interval": codes.interval,
        "timezone": codes.zone_name,
        "changes_at": code.period.end.isoformat() if code.period.end else None,
        "previous": code.previous,
        "previous_until": code.previous_until.isoformat() if code.previous_until else None,
    }
