"""Access codes (docs/access.md): whether a client needs one, and a pass for a right one."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import HTTPConnection

from .. import cluster
from ..access import Codes, is_internal
from ..lockouts import SHARED_BY, Lockout, inside_of, parse_network
from .meta import client_address, from_trusted_proxy

router = APIRouter(prefix="/api/v1/access", tags=["access"])
log = logging.getLogger(__name__)

WRONG = "that isn't the current access code"  # also the answer while a network is locked out
# Earlier versions sent the pass back as this cookie too, for loading the server's own
# pages; pages need no pass now, and POST /access removes it.
PASS_COOKIE = "texas2step_pass"
# The page's WebSockets to its own server carry the pass in their subprotocol, as a
# browser can't give them a header: "texas2step.pass.<pass>", answered with "texas2step".
WS_PROTOCOL = "texas2step"
WS_PASS = WS_PROTOCOL + ".pass."


def given_pass(conn: HTTPConnection) -> str:
    """The pass a request carries: as ?pass= (the CLI, and the page to other servers), in
    an Authorization: Bearer header (the page to its own server, keeping it out of URLs
    that proxies log), or in a WebSocket's subprotocol."""
    if token := conn.query_params.get("pass", ""):
        return token
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        return token.strip()
    for protocol in conn.headers.get("sec-websocket-protocol", "").split(","):
        if (protocol := protocol.strip()).startswith(WS_PASS):
            return protocol[len(WS_PASS) :]
    return ""


class CodeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)  # as typed or pasted
    code: str = Field(max_length=200, description="The code, with any case and separators")


class BlockIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
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


class PeerIn(BaseModel):
    """A request from another server of the cluster (cluster.py), signed in its headers."""

    sender: str = Field(alias="from", max_length=64)
    to: str = Field(max_length=64)
    action: Literal["block", "clear", "check"]
    network: str = Field("", max_length=64)
    minutes: float | None = Field(None, gt=0, le=cluster.MOST_MINUTES)
    # A block set by hand there ends passes here too; one from wrong codes doesn't. Older
    # servers send no kind, so theirs count as lockouts.
    kind: Literal["lockout", "block"] = "lockout"


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
    pass_ = pass_ or given_pass(request)  # the page sends its own server's in a header
    if codes is not None and pass_:
        expires = codes.pass_expires(pass_)
        # A block set by hand on this client's network ends its pass (app.AccessGate).
        ip, _ = client_address(request)
        if expires is not None and body["required"] and await request.app.state.blocks.cover(ip):
            expires = None
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
            await _reshare(request, outcome.network)
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
            until = datetime.fromtimestamp(outcome.until, codes.zone)
            why = f"{lockouts.after} wrong access codes in a row"
            cluster.hook_in_background(request.app, "lockout", outcome.network, until, why)
            await _reshare(request, outcome.network)
        raise HTTPException(403, WRONG)
    if note != "access code":  # a standing phrase: every use is logged, so a leaked one shows
        log.warning("%s used from %s", note, ip)
    token, expires = codes.issue()
    if PASS_COOKIE in request.cookies:  # an earlier version's: the page keeps the pass itself
        response.delete_cookie(
            PASS_COOKIE,
            path=request.scope.get("root_path", "") + "/",
            secure=_https(request),
            httponly=True,
            samesite="strict",
        )
    return {"pass": token, "expires": expires}


async def _reshare(request: Request, network: str) -> None:
    """Send a lockout that began here to the other servers: when it begins, and again
    while the attacker keeps trying here, so theirs don't run out first."""
    settings = request.app.state.settings
    if not settings.access_share_lockouts:
        return
    minutes = settings.access_lockout_minutes
    lockouts = request.app.state.lockouts
    if await asyncio.to_thread(lockouts.due_to_share, network, minutes * 60 / 3):
        cluster.share_in_background(request.app, "block", network, minutes)


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
        "by_hand": lockout.by_hand,  # a block: it ends the passes used from the network too
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
    if wide := cluster.too_wide(net, cluster.WIDEST_BY_HAND):
        raise HTTPException(400, f"{wide}; block smaller networks")
    minutes = given.minutes or settings.access_lockout_minutes
    lockouts = request.app.state.lockouts
    why = f"blocked by the API from {ip}"
    lockout = await asyncio.to_thread(lockouts.block, str(net), minutes, why)
    request.app.state.blocks.forget()
    codes = request.app.state.codes
    log.warning("%s blocked until %s, by the API from %s", net, _at(codes, lockout.expires), ip)
    until = datetime.fromtimestamp(lockout.expires, codes.zone)
    cluster.hook_in_background(request.app, "block", str(net), until, why)
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
    network = network.strip()
    try:
        target = None if network == "all" else str(parse_network(network))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    cleared = await asyncio.to_thread(request.app.state.lockouts.clear, target)
    if cleared:
        log.warning("lockouts ended by the API from %s: %s", ip, ", ".join(cleared))
        why = f"ended by the API from {ip}"
        cluster.hook_in_background(request.app, "clear", target or "all", None, why)
    return {"cleared": cleared}


# --- Servers that defend together (cluster.py) ------------------------------------


@router.post(
    "/peer",
    summary="A block, clear or check from another server of the cluster",
    responses={403: {"description": "Not signed with this cluster's key, or replayed"}},
)
async def peer(request: Request) -> dict[str, Any]:
    """Only with ``access_share_lockouts`` on, from an address in
    ``access_peer_networks``, signed with a key derived from the shared access key; 404 to
    everyone else. The body: ``{"from", "to", "action": "block" | "clear" | "check",
    "network", "minutes"}``. A block covers at most a /24 or a /48 and never
    ``internal_networks``, and isn't passed on to other servers."""
    settings = request.app.state.settings
    codes = request.app.state.codes
    ip, _ = client_address(request)
    if (
        codes is None
        or not settings.access_share_lockouts
        or not cluster.in_networks(ip, settings.access_peer_networks)
    ):
        raise HTTPException(404, "Not Found")
    raw = await request.body()
    now = time.time()
    lockouts = request.app.state.lockouts
    why = cluster.verify(cluster.peer_key(codes.key), request.headers, raw, now)
    nonce = request.headers.get("x-texas2step-nonce", "")
    if why is None and not await asyncio.to_thread(
        lockouts.first_use, nonce, 2 * cluster.SKEW, now
    ):
        why = "it was sent before"
    try:
        given = None if why else PeerIn.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(400, f"not a request this server understands: {exc}") from None
    if given is not None and given.to != settings.server_id:
        why = f"it is for {given.to}, not {settings.server_id}"
    if why or given is None:
        log.warning("request from another server at %s refused: %s", ip, why)
        raise HTTPException(403, why)
    if given.action == "check":
        return {"server": settings.server_id, "ok": True}
    by_hand = given.kind == "block"
    source = f"{SHARED_BY}{given.sender} from {ip}" + (", set by hand" if by_hand else "")
    if given.action == "clear":
        try:  # ending lockouts can't harm: any network, or all
            target = None if given.network == "all" else str(parse_network(given.network))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        cleared = await asyncio.to_thread(lockouts.clear, target)
        if cleared:
            log.warning("lockouts ended by %s: %s", source, ", ".join(cleared))
            ended = f"ended by {given.sender} from {ip}"
            cluster.hook_in_background(request.app, "clear", target or "all", None, ended)
        return {"cleared": cleared}
    net = _peer_network(given.network, settings)
    if await asyncio.to_thread(lockouts.held_from_others) >= cluster.MOST_HELD:
        log.warning(
            "block of %s from %s refused: %d from other servers held", net, ip, cluster.MOST_HELD
        )
        raise HTTPException(429, f"this server holds {cluster.MOST_HELD} blocks from others")
    minutes = given.minutes or settings.access_lockout_minutes
    lockout = await asyncio.to_thread(lambda: lockouts.block(net, minutes, source, by_hand=by_hand))
    request.app.state.blocks.forget()
    log.warning("%s blocked until %s, %s", net, _at(codes, lockout.expires), source)
    until = datetime.fromtimestamp(lockout.expires, codes.zone)
    cluster.hook_in_background(request.app, "block", net, until, source)
    return _lockout(codes, lockout)


def _peer_network(text: str, settings: Any) -> str:
    """A network another server may block here: at most a /24 or a /48, and never
    internal_networks or loopback (400 otherwise)."""
    try:
        net = parse_network(text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if inside := inside_of(net, settings.internal_networks):
        raise HTTPException(400, f"{net} overlaps {inside}, which is never locked out")
    if wide := cluster.too_wide(net, cluster.WIDEST):
        raise HTTPException(400, f"{wide}, the most another server may block")
    return str(net)
