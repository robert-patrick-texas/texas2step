"""Discovery endpoints: who is this server, who is the client, where else can I test."""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Request

from .. import __version__, rtc

router = APIRouter(prefix="/api/v1", tags=["meta"])


def client_address(request: Request) -> tuple[str, int | None]:
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
    return host, port


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
    return {
        "name": "texas2step",
        "version": __version__,
        "server": {"id": s.server_id, "name": s.server_name, "location": s.server_location},
        "capabilities": {
            "websocket": True,
            "webrtc": s.webrtc_enabled and rtc.AVAILABLE,
            "results": s.results_enabled,
            "measure": s.measure_enabled,
        },
        "limits": {
            "max_download_bytes": s.max_download_bytes,
            "max_upload_bytes": s.max_upload_bytes,
            "max_stream_seconds": s.max_stream_seconds,
        },
        "ice_servers": [{"urls": url} for url in s.ice_servers],
        "defaults": {
            "streams": s.default_streams,
            "seconds": s.default_seconds,
            "packets": s.default_packets,
        },
    }


@router.get("/whoami", summary="The client as this server sees it")
async def whoami(request: Request) -> dict[str, Any]:
    ip, port = client_address(request)
    trusted = request.client is not None and request.client.host in (
        request.app.state.settings.trusted_proxies
    )
    scheme = request.headers.get("x-forwarded-proto") if trusted else None
    protocol = request.headers.get("x-client-protocol") if trusted else None
    return {
        "ip": ip,
        "port": port,
        "family": address_family(ip),
        "scheme": scheme or request.url.scheme,
        "http_version": protocol or f"HTTP/{request.scope.get('http_version', '1.1')}",
        "user_agent": request.headers.get("user-agent"),
        "via_proxy": bool(trusted and request.headers.get("x-forwarded-for")),
    }


@router.get("/locations", summary="Servers offered in the location picker")
async def locations(request: Request) -> dict[str, Any]:
    return {"locations": [loc.public() for loc in request.app.state.settings.all_locations()]}


@router.get("/health", summary="Liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
