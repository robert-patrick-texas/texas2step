"""ASGI application factory."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__, page
from .api import routers
from .config import Settings, load_settings
from .rtc import RtcEcho
from .store import ResultStore

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
SPA_ROUTES = ("/", "/monitor", "/diag", "/history", "/api", "/r/{rid}")
PREFIX = re.compile(r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._~-]+)+")  # no "." or ".." segments


class TimingAllowOrigin:
    """Adds ``Timing-Allow-Origin: *`` so cross-origin clients can read Resource Timing.

    Written as raw ASGI middleware: ``BaseHTTPMiddleware`` would re-buffer streamed
    bodies and cap throughput.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"timing-allow-origin", b"*"))
            await send(message)

        await self.app(scope, receive, wrapped)


class ForwardedPrefix:
    """Serves the app under the path a trusted reverse proxy names in X-Forwarded-Prefix.

    nginx can publish texas2step at https://www.example.net/texas2step/. Given that
    prefix, this sets the ASGI ``root_path``, so the page's <base href>, the static
    assets and the OpenAPI docs all point back through the proxy. The proxy may strip
    the prefix from the path or pass it through. The header is ignored from untrusted
    clients and when it is not a plain path.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        prefix = self._prefix(scope) if scope["type"] in ("http", "websocket") else ""
        if prefix:
            scope = dict(scope, root_path=prefix)
            path: str = scope["path"]
            if path != prefix and not path.startswith(prefix + "/"):
                scope["path"] = prefix + path
                raw_path: bytes | None = scope.get("raw_path")
                if raw_path:
                    scope["raw_path"] = prefix.encode() + raw_path
        await self.app(scope, receive, send)

    def _prefix(self, scope: Scope) -> str:
        client = scope.get("client")
        if not client or client[0] not in self.settings.trusted_proxies:
            return ""
        for name, value in scope["headers"]:
            if name == b"x-forwarded-prefix":
                prefix = value.decode("latin-1").strip().rstrip("/")
                return prefix if len(prefix) <= 200 and PREFIX.fullmatch(prefix) else ""
        return ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.rtc.start()
        if settings.results_enabled:
            app.state.store = ResultStore(settings.results_path, settings.results_max_rows)
        yield
        await app.state.rtc.stop()
        if app.state.store is not None:
            app.state.store.close()

    app = FastAPI(
        title="texas2step",
        version=__version__,
        summary="Throughput, latency, jitter and packet-loss measurement between endpoints.",
        docs_url=None,  # both served below
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = None
    app.state.rtc = RtcEcho(
        settings.ice_servers,
        settings.max_rtc_sessions,
        settings.rtc_session_timeout,
        enabled=settings.webrtc_enabled,
        port_range=settings.rtc_ports,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Server-Timing", "Content-Length"],
        max_age=600,
    )
    app.add_middleware(TimingAllowOrigin)
    app.add_middleware(ForwardedPrefix, settings=settings)

    for router in routers:
        app.include_router(router)

    index = STATIC / "index.html"
    logo = page.logo_file(settings)
    shell = page.Shell(settings, logo)

    async def spa(request: Request) -> HTMLResponse:
        # Relative URLs in the page resolve against <base href>, which carries the prefix.
        body = shell.render(index.read_text(), request.scope.get("root_path", ""), __version__)
        return HTMLResponse(body, headers={"Cache-Control": "no-cache"})

    for route in SPA_ROUTES:
        app.add_api_route(route, spa, include_in_schema=False, methods=["GET"])

    if (STATIC / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")
    else:
        log.warning("frontend assets missing: run `npm run build` in web/")

    swagger = STATIC / "assets" / "swagger"
    stamps = {
        name: hashlib.sha256((swagger / name).read_bytes()).hexdigest()[:10]
        for name in ("swagger-ui-bundle.js", "swagger-ui.css")
        if (swagger / name).is_file()
    }

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi(request: Request) -> JSONResponse:
        # FastAPI would record the first prefix it sees in "servers" for good. Set it per
        # request instead, so "Try it out" targets the address the explorer was opened at.
        return JSONResponse(
            {**app.openapi(), "servers": [{"url": request.scope.get("root_path", "") or "/"}]}
        )

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request) -> HTMLResponse:
        # Swagger UI comes from our own assets (copied in by web/build.mjs), not a CDN, so
        # the explorer works on networks without internet access.
        root = request.scope.get("root_path", "")
        assets = f"{root}/assets/swagger"
        return get_swagger_ui_html(
            openapi_url=root + "/api/openapi.json",
            title=f"{app.title} - Swagger UI",
            swagger_js_url=f"{assets}/swagger-ui-bundle.js?v={stamps.get('swagger-ui-bundle.js')}",
            swagger_css_url=f"{assets}/swagger-ui.css?v={stamps.get('swagger-ui.css')}",
            swagger_favicon_url=f"{root}/favicon.svg",
        )

    if logo:

        @app.get("/brand/logo", include_in_schema=False)
        async def brand_logo() -> FileResponse:
            # The URL carries a content hash, so it can be cached. The policy stops an SVG
            # logo from running scripts if someone opens it directly.
            return FileResponse(
                logo,
                media_type=page.LOGO_TYPES[logo.suffix.lower()],
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                },
            )

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    return app
