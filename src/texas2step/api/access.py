"""Access codes (docs/access.md): whether a client needs one, and a pass for a right one."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..access import Codes, is_internal
from ..lockouts import Lockout, inside_of, parse_network
from .meta import client_address, from_trusted_proxy

router = APIRouter(prefix="/api/v1/access", tags=["access"])
log = logging.getLogger(__name__)

WRONG = "that isn't the current access code"  # also the answer while a network is locked out
PASS_COOKIE = "texas2step_pass"  # the pass again, for loading this server's own pages


class CodeIn(BaseModel):
    code: str = Field(max_length=200, description="The code, with any case and separators")


class BlockIn(BaseModel):
    network: str = Field(
        max_length=64,
        description="An address or network outside internal_networks, e.g. 203.0.113.0/24",
    )
    minutes: float | None = Field(
        None,
        gt=0,
        le=525_600,
        description="How long it lasts, at least: access_lockout_minutes when left out. "
        "Each code from the network while it is locked out keeps it going too.",
    )


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
async def unlock(request: Request, given: CodeIn, response: Response) -> dict[str, Any]:
    """A pass for the current code or a standing phrase, valid on every server that holds
    the same key. Send it as ``?pass=`` with each request; it also comes as a cookie, for
    loading this server's pages. Wrong codes count against the client's /24 or /48: after
    ``access_lockout_after`` in a row, its codes are refused unread, with this same 403,
    until ``access_lockout_minutes`` pass without one."""
    codes = request.app.state.codes
    if codes is None:
        raise HTTPException(404, "this server doesn't ask for an access code")
    ip, _ = client_address(request)
    now = datetime.now(UTC)
    lockouts = request.app.state.lockouts
    if is_internal(ip, request.app.state.settings.internal_networks):
        right, note = verdict(codes, given.code, now)  # the desk trying a code: not counted
        outcome = None
    else:
        outcome = await asyncio.to_thread(
            lockouts.submit, ip, lambda: verdict(codes, given.code, now)
        )
        if outcome.locked:
            log.warning(
                "access code from %s ignored: %s is locked out until %s",
                ip,
                outcome.network,
                _at(codes, outcome.until),
            )
            raise HTTPException(403, WRONG)
        right, note = outcome.right, outcome.note
    if not right:
        log.warning("%s from %s", note, ip)
        if outcome and outcome.locked_now:
            log.warning(
                "%s locked out after %d wrong access codes in a row, until %s",
                outcome.network,
                lockouts.after,
                _at(codes, outcome.until),
            )
        raise HTTPException(403, WRONG)
    if note != "access code":  # a standing phrase: every use is logged, so a leaked one shows
        log.warning("%s used from %s", note, ip)
    token, expires = codes.issue()
    response.set_cookie(
        PASS_COOKIE,
        token,
        max_age=codes.pass_seconds,
        path=request.scope.get("root_path", "") + "/",
        secure=_https(request),
        httponly=True,
        samesite="strict",
    )
    return {"pass": token, "expires": expires}


def verdict(codes: Codes, text: str, now: datetime) -> tuple[bool, str]:
    """Whether ``text`` gets a pass, and what it was, for the journal."""
    if codes.check(text, now):
        return True, "access code"
    phrase = codes.phrase(text)
    if phrase is None:
        return False, "wrong access code"
    if codes.expired(phrase, now):
        return False, f"expired access phrase {phrase.name}"
    return True, f"access phrase {phrase.name}"


def _https(request: Request) -> bool:
    if from_trusted_proxy(request):
        return request.headers.get("x-forwarded-proto") == "https"
    return request.url.scheme == "https"


def _at(codes: Codes, moment: float) -> str:
    return f"{datetime.fromtimestamp(moment, codes.zone):%Y-%m-%d %H:%M %Z}"


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


# --- Lockouts and blocks, for the desk and its automation ---------------------------


def _desk_client(request: Request) -> str:
    """The caller's address, if it is internal; 404 to everyone else, as for the code."""
    ip, _ = client_address(request)
    if request.app.state.codes is None or not is_internal(
        ip, request.app.state.settings.internal_networks
    ):
        raise HTTPException(404, "Not Found")
    return ip


def _lockout(codes: Codes, lockout: Lockout) -> dict[str, Any]:
    since = lockout.locked_at
    return {
        "network": lockout.network,
        "source": lockout.source,
        "wrong_codes": lockout.failures,
        "since": datetime.fromtimestamp(since, codes.zone).isoformat() if since else None,
        "until": datetime.fromtimestamp(lockout.expires, codes.zone).isoformat(),
    }


@router.get("/blocks", summary="Networks locked out of entering codes")
async def blocks(request: Request) -> dict[str, Any]:
    """Lockouts after wrong codes and blocks set by hand, with when each began and ends;
    ``counting``: networks with wrong codes in a row, not locked out yet. Only for
    clients in ``internal_networks`` and on the server itself."""
    _desk_client(request)
    locked, counting = await asyncio.to_thread(request.app.state.lockouts.current)
    codes = request.app.state.codes
    return {"lockouts": [_lockout(codes, lockout) for lockout in locked], "counting": counting}


@router.post("/blocks", summary="Lock a network out of entering codes")
async def block(request: Request, given: BlockIn) -> dict[str, Any]:
    """As if it had entered too many wrong codes, for as long as ``minutes``. Send it
    again to keep a block going, such as to every server when one of them sees an attack.
    A network that overlaps ``internal_networks`` or loopback is refused (400)."""
    ip = _desk_client(request)
    settings = request.app.state.settings
    try:
        net = parse_network(given.network)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if inside := inside_of(net, settings.internal_networks):
        raise HTTPException(400, f"{net} overlaps {inside}, which is never locked out")
    minutes = given.minutes or settings.access_lockout_minutes
    lockouts = request.app.state.lockouts
    lockout = await asyncio.to_thread(
        lockouts.block, str(net), minutes, f"blocked by the API from {ip}"
    )
    codes = request.app.state.codes
    log.warning("%s blocked until %s, by the API from %s", net, _at(codes, lockout.expires), ip)
    return _lockout(codes, lockout)


@router.delete("/blocks", summary="End lockouts and blocks early")
async def unblock(
    request: Request,
    network: str = Query(
        max_length=64,
        description="An address or network: each lockout that overlaps it ends. 'all' ends "
        "every one.",
    ),
) -> dict[str, Any]:
    ip = _desk_client(request)
    try:
        target = None if network == "all" else str(parse_network(network))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    cleared = await asyncio.to_thread(request.app.state.lockouts.clear, target)
    if cleared:
        log.warning("lockouts ended by the API from %s: %s", ip, ", ".join(cleared))
    return {"cleared": cleared}
