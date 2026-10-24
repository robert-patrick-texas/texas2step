"""ASGI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from . import __version__, page, traffic
from .access import Codes, is_internal
from .api import routers
from .api.access import PASS_COOKIE
from .api.meta import client_address, http_version, shows_version
from .api.results import MAX_RESULT_BYTES
from .config import Settings, load_settings
from .lockouts import LockoutStore
from .rtc import RtcEcho
from .store import ResultStore
from .traffic import TrafficMeter

log = logging.getLogger(__name__)
access = logging.getLogger("texas2step.access")

STATIC = Path(__file__).parent / "static"
SPA_ROUTES = ("/", "/monitor", "/diag", "/history", "/api", "/r/{rid}")
PREFIX = re.compile(r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._~-]+)+")  # no "." or ".." segments
# The first message of a response, which settles its status for RequestLog. A WebSocket
# closed before it was accepted gets a 403 from uvicorn.
RESPONSE_STARTS = ("http.response.start", "websocket.http.response.start")
WEBSOCKET_STATUS = {"websocket.accept": 101, "websocket.close": 403}
PASS_PARAM = re.compile(rb"(^|&)pass=[^&]*")


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


class AccessGate:
    """Asks visitors from outside ``internal_networks`` for an access pass before tests,
    when ``access_code`` is on, and keeps the service desk's functions to those networks
    when they are set (docs/access.md). It sits inside CORSMiddleware, so a page on
    another server can read the refusal. Raw ASGI, like TimingAllowOrigin.
    """

    # /access/peer checks its caller itself: an address in access_peer_networks and a
    # signature from the cluster's key (cluster.py).
    PUBLIC = ("/api/v1/health", "/api/v1/access", "/api/v1/access/peer")
    DESK_PAGES = (
        "/history",
        "/api",
        "/api/docs",
        "/api/openapi.json",
        "/api/v1/access/code",
        "/api/v1/access/blocks",
    )
    # Pages besides /, which asks for the code: from outside, only with a pass. A page
    # load can't carry ?pass=, so the pass comes as the cookie POST /access sets too.
    PAGES = ("/monitor", "/diag", "/history", "/api", "/api/docs", "/api/openapi.json")

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    @classmethod
    def desk_only(cls, method: str, path: str) -> bool:
        if path in cls.DESK_PAGES or path.startswith(("/r/", "/api/v1/measure/")):
            return True
        # Saving a result stays open, so a visitor can save a test for the desk to read.
        results = path == "/api/v1/results" or path.startswith("/api/v1/results/")
        return results and method != "POST"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        codes: Codes | None = scope["app"].state.codes if "app" in scope else None
        networks = self.settings.internal_networks
        if scope["type"] not in ("http", "websocket") or (codes is None and not networks):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        path, root = scope["path"], scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        ip, _ = client_address(conn)
        inside = is_internal(ip, networks)
        if networks and not inside and self.desk_only(scope.get("method", "GET"), path):
            await self._refuse(scope, receive, send, 404, "Not Found")
            return
        page = path in self.PAGES or path.startswith("/r/")
        if codes and not inside and page and not held(codes, conn):
            await self._home(scope, receive, send, root)
            return
        needs_pass = (
            codes and not inside and path.startswith("/api/v1/") and path not in self.PUBLIC
        )
        if needs_pass and codes.pass_expires(conn.query_params.get("pass", "")) is None:
            await self._refuse(scope, receive, send, 401, "access code required")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _home(scope: Scope, receive: Receive, send: Send, root: str) -> None:
        """To the page that asks for the code."""
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008, reason="access code required")(scope, receive, send)
            return
        headers = {"Location": f"{root}/", "Cache-Control": "no-store"}
        await Response(status_code=303, headers=headers)(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, why: str) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008, reason=why)(scope, receive, send)
            return
        response = JSONResponse({"detail": why}, status, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)


def held(codes: Codes, conn: HTTPConnection) -> bool:
    """Whether a page load comes with a current pass, in its cookie (or ?pass=)."""
    token = conn.cookies.get(PASS_COOKIE) or conn.query_params.get("pass", "")
    return codes.pass_expires(token) is not None


class BodyLimit:
    """Refuses a request body larger than its endpoint takes (413), before it is read.

    Only the upload test takes any size. Without this, a body of hundreds of megabytes
    sent to POST /api/v1/access, which needs no code, would sit in the worker's memory.
    The bodies it allows are small, so it reads them whole, then hands them on.
    """

    UNLIMITED = ("/api/v1/up",)
    LIMITS: ClassVar[dict[str, int]] = {
        "/api/v1/access": 1024,
        "/api/v1/access/blocks": 1024,
        "/api/v1/access/peer": 1024,
        "/api/v1/results": MAX_RESULT_BYTES,
    }
    DEFAULT = 64 * 1024

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        path, root = scope["path"], scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        if path in self.UNLIMITED:
            await self.app(scope, receive, send)
            return
        limit = self.LIMITS.get(path, self.DEFAULT)
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > limit:
            await self._too_large(scope, receive, send, limit)
            return
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > limit:
                await self._too_large(scope, receive, send, limit)
                return
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        why = f"request body too large: this endpoint takes up to {limit} bytes"
        await JSONResponse({"detail": why}, 413)(scope, receive, send)


class RequestLog:
    """Logs one line per request, with the client's address as /api/v1/whoami sees it.

    Behind nginx, uvicorn's own line (turned off by ``texas2step serve``) would show
    127.0.0.1 for everyone. Failures log as warnings; successes as info, which only
    ``debug = true`` writes (see logs.py). Raw ASGI, like TimingAllowOrigin.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        logged = False

        async def wrapped(message: Message) -> None:
            nonlocal logged
            kind = message["type"]
            if not logged and (kind in RESPONSE_STARTS or kind in WEBSOCKET_STATUS):
                logged = True
                self._line(scope, message.get("status") or WEBSOCKET_STATUS[kind])
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            if not logged:  # Starlette answers 500 and uvicorn logs the traceback
                self._line(scope, 500)
            raise

    @staticmethod
    def _line(scope: Scope, status: int) -> None:
        # 499: the browser cancelled an upload when a timed test ended, as it should.
        level = logging.WARNING if status >= 400 and status != 499 else logging.INFO
        if not access.isEnabledFor(level):
            return
        conn = HTTPConnection(scope)
        ip, _ = client_address(conn)
        # The raw bytes, escaped: a decoded path could hold a newline and fake a line.
        target = scope.get("raw_path") or scope["path"].encode()
        if scope.get("query_string"):
            # An access pass would let anyone who reads the journal test until it expires.
            target += b"?" + PASS_PARAM.sub(rb"\1pass=*", scope["query_string"])
        target = target.decode("ascii", "backslashreplace")
        if scope["type"] == "websocket":
            request = f"WebSocket {target}"
        else:
            request = f"{scope['method']} {target} {http_version(conn)}"
        access.log(level, '%s - "%s" %d', ip, request, status)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.rtc.start()
        if settings.results_enabled:
            app.state.store = ResultStore(settings.results_path, settings.results_max_rows)
        meter = app.state.traffic
        flushing = asyncio.create_task(traffic.keep(meter)) if meter is not None else None
        yield
        await app.state.rtc.stop()
        if app.state.store is not None:
            app.state.store.close()
        if app.state.lockouts is not None:
            app.state.lockouts.close()
        if flushing is not None:
            flushing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flushing
            meter.flush()
            meter.close()

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
    app.state.codes = Codes.load(settings)
    app.state.lockouts = None
    if app.state.codes is not None:  # opened here, so a file that can't be used stops the start
        app.state.lockouts = LockoutStore(
            settings.access_lockouts_file,
            settings.access_lockout_after,
            settings.access_lockout_minutes,
        )
    app.state.traffic = TrafficMeter.load(settings)
    app.state.tasks = set()  # sharing lockouts and running the hook, in the background
    app.state.store = None
    app.state.rtc = RtcEcho(
        settings.ice_servers,
        settings.max_rtc_sessions,
        settings.rtc_session_timeout,
        enabled=settings.webrtc_enabled,
        port_range=settings.rtc_ports,
        video=settings.monitor_video,
    )
    app.state.rtc.meter = app.state.traffic

    @app.exception_handler(traffic.Closed)
    async def closed(request: Request, exc: traffic.Closed) -> JSONResponse:
        # A traffic limit: the site's message and link, and not when tests start again.
        return JSONResponse(exc.notice, 503, headers={"Cache-Control": "no-store"})

    app.add_middleware(BodyLimit)  # inside the gate: a refused request is never read
    app.add_middleware(AccessGate, settings=settings)  # inside CORS: its refusals get CORS headers
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Server-Timing", "Content-Length"],
        max_age=600,
    )
    app.add_middleware(TimingAllowOrigin)
    app.add_middleware(ForwardedPrefix, settings=settings)
    app.add_middleware(RequestLog)  # outermost: sees the request as it arrived

    for router in routers:
        app.include_router(router)

    index = STATIC / "index.html"
    logo = page.logo_file(settings)
    shell = page.Shell(settings, logo)

    async def spa(request: Request) -> HTMLResponse:
        # Relative URLs in the page resolve against <base href>, which carries the prefix.
        # Visitors from outside internal_networks don't get the desk's tabs.
        ip, _ = client_address(request)
        outside = settings.internal_networks and not is_internal(ip, settings.internal_networks)
        hide = page.DESK_PAGES if outside else frozenset()
        root = request.scope.get("root_path", "")
        codes = request.app.state.codes
        # Not the version for a visitor who hasn't entered the code: it would tell an
        # attacker which flaws to try. The page gets it from /api/v1/info once in.
        known = shows_version(request) or held(codes, request)
        body = shell.render(index.read_text(), root, __version__ if known else "", hide)
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
