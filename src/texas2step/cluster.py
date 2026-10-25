"""Servers that defend together: lockouts and blocks shared between them (docs/access.md).

With ``access_share_lockouts`` on, a server that locks a network out after wrong codes
sends a block to every server in ``[[locations]]``, and ``texas2step access block`` and
``access lockouts --clear`` go to each of them too. While the attacker keeps trying here,
the block is sent again, so the others' don't run out first.

A server takes such a request only if it comes from an address in
``access_peer_networks`` and carries an HMAC-SHA-256 signature, with a key derived from
the access key the servers already share, over the time, a one-time value and the body.
The body names the server it is for, and a request older than ``SKEW`` seconds, or seen
before, is refused. HMAC with a 256-bit key holds against quantum computers too (they
halve a symmetric key's strength; they break public-key cryptography). What one server
can make another do is bounded, against a buggy or compromised one: a block covers at
most a /24 or a /48, never ``internal_networks`` or loopback, lasts at most a week, and is
never passed on; and a server holds at most ``MOST_HELD`` blocks from the others. One
can end every pass issued before now on the others (``texas2step access passes --end``),
which at worst has everyone enter the code again.

``access_lockout_hook`` names a program run on each lockout, block and clear here, such as
to tell a SIEM or a firewall's API.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

PATH = "/api/v1/access/peer"
SKEW = 300  # seconds a request may be old, or ahead: the servers' clocks must agree
MOST_HELD = 1000  # blocks from other servers one server takes at once
WIDEST = {4: 24, 6: 48}  # the widest network a shared block may cover: one lockout group
WIDEST_BY_HAND = {4: 16, 6: 32}  # and one set by hand, on this server alone
MOST_MINUTES = 10080  # a week: the longest a shared block lasts
TIMEOUT = 5.0  # seconds to wait for each other server
HOOK_SECONDS = 30  # how long the hook may run
CONTEXT = b"texas2step peer request v1"
HEADERS = ("x-texas2step-time", "x-texas2step-nonce", "x-texas2step-signature")


def peer_key(access_key: bytes) -> bytes:
    """The key for requests between servers: HKDF-SHA-256 (RFC 5869) of the access key,
    so the access key itself never signs anything that crosses the network."""
    prk = hmac.new(b"texas2step", access_key, hashlib.sha256).digest()
    return hmac.new(prk, CONTEXT + b"\x01", hashlib.sha256).digest()


def sign(key: bytes, body: bytes, stamp: int, nonce: str) -> str:
    message = b"\n".join((CONTEXT, str(stamp).encode(), nonce.encode(), body))
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def signed(key: bytes, body: bytes, now: float | None = None) -> dict[str, str]:
    """The headers that make ``body`` a request another server of the cluster takes."""
    stamp = int(now if now is not None else time.time())
    nonce = secrets.token_hex(16)
    return {
        "x-texas2step-time": str(stamp),
        "x-texas2step-nonce": nonce,
        "x-texas2step-signature": sign(key, body, stamp, nonce),
    }


def verify(key: bytes, headers: Mapping[str, str], body: bytes, now: float) -> str | None:
    """Why the request isn't a signed one from the cluster, or None if it is. The caller
    still checks that its one-time value is new."""
    stamp, nonce, signature = (headers.get(name, "") for name in HEADERS)
    if not (stamp.isdigit() and len(nonce) == 32 and len(signature) == 64):
        return "not a signed request from another server"
    if abs(now - int(stamp)) > SKEW:
        return f"its time is more than {SKEW // 60} minutes off this server's clock"
    if not hmac.compare_digest(sign(key, body, int(stamp), nonce), signature):
        return "its signature doesn't match: does that server hold a different access key?"
    return None


def too_wide(net: ipaddress.IPv4Network | ipaddress.IPv6Network, widest: dict[int, int]) -> str:
    """Why ``net`` is wider than a block may be, or ""."""
    limit = widest[net.version]
    return f"{net} is wider than a /{limit}" if net.prefixlen < limit else ""


def in_networks(ip: str, networks: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in ipaddress.ip_network(net, strict=False) for net in networks)


# --- Sending -----------------------------------------------------------------------


async def send(
    settings: Any,
    key: bytes,
    action: str,
    network: str,
    minutes: float = 0,
    kind: str = "lockout",
    at: float = 0,
) -> list[tuple[str, str | None]]:
    """Send a block, a clear, an end of passes or a check to every server in [[locations]],
    at once: each one's id, and why it didn't take it (None when it did). A block's
    ``kind`` is "lockout" for one from wrong codes, "block" for one set by hand, which ends
    passes. An end of passes ("end-passes") ends those issued before ``at``."""
    peers = [loc for loc in settings.locations if loc.url]
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers={"user-agent": f"texas2step-peer/{__version__}"}
    ) as client:
        answers = await asyncio.gather(
            *(
                _send_one(client, settings, key, loc, action, network, minutes, kind, at)
                for loc in peers
            )
        )
    return [(loc.id, why) for loc, why in zip(peers, answers, strict=True)]


def summary(results: list[tuple[str, str | None]]) -> str:
    """Such as "sent to houston; not to dallas (timed out)"."""
    if not results:
        return "not sent: no other servers in [[locations]]"
    took = [peer for peer, why in results if why is None]
    refused = [f"{peer} ({why})" for peer, why in results if why]
    parts = [f"sent to {', '.join(took)}"] if took else []
    if refused:
        parts.append(f"not to {'; '.join(refused)}")
    return "; ".join(parts)


async def _send_one(
    client: httpx.AsyncClient,
    settings: Any,
    key: bytes,
    loc: Any,
    action: str,
    network: str,
    minutes: float,
    kind: str,
    at: float,
) -> str | None:
    body = {"from": settings.server_id, "to": loc.id, "action": action, "network": network}
    if minutes:
        body["minutes"] = minutes
    if at:
        body["at"] = at
    if action == "block":
        body["kind"] = kind  # older servers ignore it
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", **signed(key, raw)}
    try:
        r = await client.post(loc.url.rstrip("/") + PATH, content=raw, headers=headers)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if r.status_code == 200:
        return None
    if r.status_code == 404:
        return (
            "404: sharing is off there, or this server's address isn't in its access_peer_networks"
        )
    try:
        detail = r.json().get("detail") or r.reason_phrase
    except ValueError:
        detail = r.reason_phrase
    return f"{r.status_code}: {detail}"


def share_in_background(app: Any, action: str, network: str, minutes: float = 0) -> None:
    """Send from the service without holding up the request that set it off."""
    settings = app.state.settings
    if not settings.access_share_lockouts or app.state.codes is None:
        return

    async def go() -> None:
        results = await send(settings, peer_key(app.state.codes.key), action, network, minutes)
        log.warning("%s of %s %s", action, network, summary(results))

    background(app, go())


# --- The hook ----------------------------------------------------------------------


def hook_env(event: str, network: str, until: datetime | None, why: str, server: str) -> dict:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TEXAS2STEP_EVENT": event,
        "TEXAS2STEP_NETWORK": network,
        "TEXAS2STEP_UNTIL": until.isoformat() if until else "",
        "TEXAS2STEP_WHY": why,
        "TEXAS2STEP_SERVER": server,
    }


async def run_hook(
    hook: str, event: str, network: str, until: datetime | None, why: str, server: str
) -> None:
    """Run ``access_lockout_hook`` from the service, as its user: EVENT NETWORK UNTIL."""
    env = hook_env(event, network, until, why, server)
    try:
        proc = await asyncio.create_subprocess_exec(
            hook,
            event,
            network,
            env["TEXAS2STEP_UNTIL"],
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        log.warning("access_lockout_hook %s didn't start: %s", hook, exc.strerror or exc)
        return
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), HOOK_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning(
            "access_lockout_hook %s stopped after %d s (%s %s)", hook, HOOK_SECONDS, event, network
        )
        return
    if proc.returncode:
        tail = out.decode(errors="replace").strip()[-300:]
        log.warning(
            "access_lockout_hook %s exited %d (%s %s): %s",
            hook,
            proc.returncode,
            event,
            network,
            tail,
        )


def hook_in_background(
    app: Any, event: str, network: str, until: datetime | None, why: str
) -> None:
    settings = app.state.settings
    if settings.access_lockout_hook:
        hook = run_hook(
            settings.access_lockout_hook, event, network, until, why, settings.server_id
        )
        background(app, hook)


def run_hook_now(settings: Any, event: str, network: str, until: datetime | None, why: str) -> str:
    """Run the hook from the command line, as the service's user when that is root, so it
    runs as it would in the service. A line saying how it went."""
    hook = settings.access_lockout_hook
    if not hook:
        return ""
    env = hook_env(event, network, until, why, settings.server_id)
    owner: dict[str, Any] = {}
    if os.geteuid() == 0:
        state = Path(settings.access_lockouts_file).parent
        if state.is_dir():
            folder = state.stat()
            owner = {"user": folder.st_uid, "group": folder.st_gid, "extra_groups": []}
    try:
        done = subprocess.run(
            [hook, event, network, env["TEXAS2STEP_UNTIL"]],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=HOOK_SECONDS,
            check=False,
            **owner,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"access_lockout_hook {hook}: {exc}"
    if done.returncode:
        tail = (done.stdout + done.stderr).decode(errors="replace").strip()[-300:]
        return f"access_lockout_hook {hook} exited {done.returncode}: {tail}"
    return f"access_lockout_hook {hook} ran"


def background(app: Any, work: Any) -> None:
    """Run ``work`` on the service's loop, keeping it referenced until it is done."""
    tasks: set[asyncio.Task] = app.state.tasks
    task = asyncio.get_running_loop().create_task(work)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
