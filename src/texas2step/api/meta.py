"""Discovery endpoints: who is this server, who is the client, where else can I test."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from .. import __version__, rtc
from ..access import is_internal
from ..tcpinfo import DIAG, Ends

router = APIRouter(prefix="/api/v1", tags=["meta"])

# Headers only a proxy adds. A trusted proxy that sends one of them but not the visitor's
# address has lost it, so the request mustn't count as the proxy's own, local, address.
PROXY_HEADERS = ("x-forwarded-proto", "x-forwarded-prefix", "x-forwarded-host", "forwarded")


def client_address(request: HTTPConnection) -> tuple[str, int | None]:
    """The client's address, honouring X-Forwarded-For only from trusted proxies."""
    settings = request.app.state.settings
    host, port = (request.client.host, request.client.port) if request.client else ("unknown", None)
    if host in settings.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        real = request.headers.get("x-real-ip")
        if forwarded:
            return forwarded.split(",")[0].strip(), None
        if real:
            return real.strip(), None
        if any(name in request.headers for name in PROXY_HEADERS):
            return "unknown", None
    return host, port


def from_trusted_proxy(request: HTTPConnection) -> bool:
    """Whether the peer is a proxy whose X-Forwarded-* and similar headers we believe."""
    return request.client is not None and request.client.host in (
        request.app.state.settings.trusted_proxies
    )


def http_version(request: HTTPConnection) -> str:
    """The HTTP version the client used: the proxy's word for it, else our own."""
    protocol = request.headers.get("x-client-protocol") if from_trusted_proxy(request) else None
    return protocol or f"HTTP/{request.scope.get('http_version', '1.1')}"


def address_family(ip: str) -> str | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return "IPv4"
    return f"IPv{addr.version}"


@router.get("/info", summary="Server identity, capabilities and limits")
async def info(request: Request) -> dict[str, Any]:
    s = request.app.state.settings
    out = {
        "name": "texas2step",
        "version": __version__,
        "server": {"id": s.server_id, "name": s.server_name, "location": s.server_location},
        "capabilities": {
            "websocket": True,
            "webrtc": s.webrtc_enabled and rtc.AVAILABLE,
            # The UDP probe's echoes can carry the server's stamps (docs/api.md).
            "stamps": s.webrtc_enabled and rtc.AVAILABLE,
            "results": s.results_enabled,
            "measure": s.measure_enabled,
            # GET /api/v1/tests/{id}: whether the server's CPU held a transfer back, and
            # how long the client held each download back (device_window).
            "test_cpu": True,
            "test_window": s.device_window and DIAG.available,
        },
        "limits": {
            "max_download_bytes": s.max_download_bytes,
            "max_upload_bytes": s.max_upload_bytes,
            "max_stream_seconds": s.max_stream_seconds,
            # 0: sessions run until the client closes them, or go idle.
            "max_session_seconds": s.session_max_runtime,
            # How long Monitor and Diag run; 0 for no limit. A page from another server that
            # tests this one keeps the stricter of its own site's and these.
            "max_monitor_seconds": s.page_monitor_max_runtime,
            "max_diag_seconds": s.page_diag_max_runtime,
        },
        "ice_servers": [{"urls": url} for url in s.ice_servers],
        "defaults": {
            "streams": s.default_streams,
            "seconds": s.default_seconds,
            "packets": s.default_packets,
            "one_stream": s.default_one_stream,
        },
        # The Test page's own figures for when the device is the limit, and the share of a
        # download held back that reads "Device-bound" (0: never).
        "device": {
            "check": s.device_check,
            "details": s.device_details,
            "bound_percent": s.device_bound_percent,
        },
        # Monitor's profile until a visitor picks one, and whether the call ones are offered.
        "monitor": {
            "profile": s.monitor_profile,
            "voice": s.monitor_voice,
            "video": s.monitor_video,
        },
    }
    # With internal_networks set, the version is theirs only. Without them, anyone who
    # reaches this has entered the code or needs none (AccessGate).
    if s.internal_networks and not shows_version(request):
        del out["version"]
    if (meter := request.app.state.traffic) is not None:
        # Whether a traffic limit lets this client test; for the desk, how much is left.
        kind = meter.kind(client_address(request)[0])
        out["traffic"] = await asyncio.to_thread(meter.public, kind)
    return out


@router.get("/whoami", summary="The client as this server sees it")
async def whoami(request: Request) -> dict[str, Any]:
    ip, port = client_address(request)
    trusted = from_trusted_proxy(request)
    scheme = request.headers.get("x-forwarded-proto") if trusted else None
    return {
        "ip": ip,
        "port": port,
        "family": address_family(ip),
        "scheme": scheme or request.url.scheme,
        "http_version": http_version(request),
        "user_agent": request.headers.get("user-agent"),
        "via_proxy": bool(trusted and request.headers.get("x-forwarded-for")),
    }


# What nginx can tell us about the connection, checked before it is believed: a proxy
# misconfigured to pass a client's own headers through must not put junk on the page.
_TEXT = re.compile(r"[\x21-\x7e]{1,64}")


def _ip(value: str | None) -> str | None:
    try:
        addr = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def _port(value: str | int | None) -> int | None:
    try:
        port = int(str(value).strip())
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _TEXT.fullmatch(value) else None


def _ms(value: str | None) -> float | None:
    """nginx's $tcpinfo_rtt and $tcpinfo_rttvar, in microseconds, as milliseconds."""
    try:
        micros = int((value or "").strip())
    except ValueError:
        return None
    return round(micros / 1000, 3) if 0 < micros < 600_000_000 else None


def connection_ends(request: HTTPConnection) -> Ends | None:
    """Both ends of the TCP connection that carries this request to the client: nginx's,
    from the headers the package's snippet sets, believed only from ``trusted_proxies``;
    or our own. None over HTTP/3, which isn't TCP, or when a proxy doesn't say."""
    h = request.headers
    proxied = "x-forwarded-for" in h or "x-real-ip" in h or any(n in h for n in PROXY_HEADERS)
    if from_trusted_proxy(request) and proxied:
        if (h.get("x-client-protocol") or "").upper().startswith("HTTP/3"):
            return None
        ip, port = _ip(client_address(request)[0]), _port(h.get("x-client-port"))
        server_ip, server_port = _ip(h.get("x-server-addr")), _port(h.get("x-server-port"))
        if ip is None or port is None or server_ip is None or server_port is None:
            return None
        return server_ip, server_port, ip, port
    local, peer = request.scope.get("server"), request.client
    if not local or peer is None or _ip(local[0]) is None or _ip(peer.host) is None:
        return None
    if _port(local[1]) is None or _port(peer.port) is None:
        return None
    return local[0], local[1], peer.host, peer.port


@router.get("/diag/http", summary="Both ends of this HTTP connection, for the Diag tab")
async def diag_http(request: Request) -> JSONResponse:
    """The client and server addresses of this request's connection, its HTTP version,
    TLS, and the server kernel's TCP round-trip time, with the server's clock.

    Behind nginx these come from headers the package's snippet sets (X-Client-Port,
    X-Server-Addr, X-Server-Port, X-TLS-Protocol, X-TLS-Cipher, X-TCP-RTT and
    X-TCP-RTTVAR), believed only from ``trusted_proxies``. A proxy that doesn't send
    them leaves those fields null. Without a proxy, the socket's own addresses are used.
    """
    h = request.headers
    trusted = from_trusted_proxy(request)
    ip, port = client_address(request)
    if trusted:
        port = _port(h.get("x-client-port")) or port
        server_ip, server_port = _ip(h.get("x-server-addr")), _port(h.get("x-server-port"))
        protocol, cipher = _text(h.get("x-tls-protocol")), _text(h.get("x-tls-cipher"))
        tls = {"protocol": protocol, "cipher": cipher} if protocol and cipher else None
        rtt, rttvar = _ms(h.get("x-tcp-rtt")), _ms(h.get("x-tcp-rttvar"))
    else:
        local = request.scope.get("server") or (None, None)
        server_ip, server_port = _ip(local[0]), _port(local[1])
        tls, rtt, rttvar = None, None, None
    body = {
        "client": {"ip": ip, "port": port, "family": address_family(ip)},
        "server": {
            "ip": server_ip,
            "port": server_port,
            "family": address_family(server_ip) if server_ip else None,
        },
        "http_version": http_version(request),
        "tls": tls,
        "tcp_rtt_ms": rtt,
        "tcp_rttvar_ms": rttvar,
        "time_ms": time.time() * 1000,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/locations", summary="Servers offered in the location picker")
async def locations(request: Request) -> dict[str, Any]:
    return {"locations": [loc.public() for loc in request.app.state.settings.all_locations()]}


@router.get("/health", summary="Liveness check")
async def health(request: Request) -> dict[str, str]:
    """The version too, except to visitors who would need an access code."""
    if shows_version(request):
        return {"status": "ok", "version": __version__}
    return {"status": "ok"}


def shows_version(request: HTTPConnection) -> bool:
    """Whether this client may see the version: knowing it would tell an attacker which
    flaws to try. With internal_networks set, only they do; without them, not a visitor
    who needs an access code; on a server open to all, everyone."""
    networks = request.app.state.settings.internal_networks
    if request.app.state.codes is None and not networks:
        return True
    ip, _ = client_address(request)
    return is_internal(ip, networks)
