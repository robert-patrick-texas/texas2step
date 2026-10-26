"""ASGI application factory."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from . import __version__, page, traffic
from .access import Codes, is_internal
from .api import routers
from .api.access import given_pass
from .api.meta import client_address, http_version, shows_version
from .api.results import MAX_RESULT_BYTES
from .config import Settings, load_settings
from .lockouts import HandBlocks, LockoutStore
from .rtc import RtcEcho
from .store import ResultStore
from .traffic import TrafficMeter

log = logging.getLogger(__name__)
access = logging.getLogger("texas2step.access")

STATIC = Path(__file__).parent / "static"
SPA_ROUTES = ("/", "/monitor", "/diag", "/history", "/help", "/api", "/r/{rid}")
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


# A script written into a page, not loaded from a file: allowed by its hash.
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def server_origins(settings: Settings) -> list[str]:
    """The other servers the page reaches, for its Content-Security-Policy: each in the
    picker and this server's own single-family names, over HTTP and WebSocket."""
    urls = settings.all_locations()[0].urls()
    for loc in settings.locations:
        urls += loc.urls()
    origins: list[str] = []
    for url in filter(None, urls):
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            ws = "wss" if parts.scheme == "https" else "ws"
            origins += [f"{parts.scheme}://{parts.netloc}", f"{ws}://{parts.netloc}"]
    return list(dict.fromkeys(origins))


def content_policy(html: str, origins: list[str]) -> str:
    """A page's Content-Security-Policy: scripts only its own files and those written into
    it, by hash, and connections only to texas2step servers, so a script injected some
    other way can't run, or send what it finds elsewhere. Styles may be inline, as the app
    sets each chart's colours on its elements; images may be data: URIs, as in the CSS."""
    hashes = [
        f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'"
        for s in INLINE_SCRIPT.findall(html)
    ]
    return "; ".join(
        [
            "default-src 'self'",
            " ".join(["script-src 'self'", *hashes]),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            " ".join(["connect-src 'self'", *origins]),
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
    )


class NoStore:
    """``Cache-Control: no-store`` on every API answer that doesn't say otherwise. They are
    made for each request, and some hold callers' addresses (saved results), the current
    code or a pass: a browser that opened one directly, or through the API explorer, would
    otherwise keep a copy in its disk cache. The page asks for none to be kept either.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        root = scope.get("root_path", "")
        path = scope.get("path", "")
        path = path[len(root) :] if root and path.startswith(root) else path
        if scope["type"] != "http" or not path.startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
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
        method = scope.get("method", "GET")
        if networks and not inside and self.desk_only(method, path):
            await self._refuse(scope, receive, send, 404, "Not Found")
            return
        if networks and self.desk_only(method, path) and self.cross_site(conn, method):
            await self._refuse(scope, receive, send, 403, "cross-site request refused")
            return
        # Pages need no pass: each is the same app as /, which asks for the code itself,
        # and everything it fetches or opens needs one.
        needs_pass = (
            codes and not inside and path.startswith("/api/v1/") and path not in self.PUBLIC
        )
        # A pass issued before passes were last ended, or from a network blocked by hand,
        # is no pass: the page drops it and asks for the code, which a blocked network
        # can't enter until the block ends.
        ends = scope["app"].state.blocks if needs_pass else None
        if ends is not None and (
            codes.pass_expires(given_pass(conn), ended=await ends.ended()) is None
            or await ends.cover(ip)
        ):
            await self._refuse(scope, receive, send, 401, "access code required")
            return
        await self.app(scope, receive, send)

    @staticmethod
    def cross_site(conn: HTTPConnection, method: str) -> bool:
        """Whether a browser sent this for a page on another site. The desk's functions go
        by the address a request comes from, and CORS lets any page read the answer, so a
        site an employee opens inside could otherwise read History through their browser.
        Requests without these headers are a script's, the CLI's or monitoring's. A link
        from elsewhere (a navigation) still opens the desk's pages: the other site can't
        read them."""
        headers = conn.headers
        if method == "GET" and headers.get("sec-fetch-mode") == "navigate":
            return False
        site = headers.get("sec-fetch-site")
        if site is not None:
            return site not in ("same-origin", "none")
        # Browsers without Fetch Metadata still name the page's origin.
        origin = headers.get("origin")
        if origin is None:
            return False
        host = urlsplit("//" + headers.get("host", "")).hostname
        return urlsplit(origin).hostname != host

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, why: str) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008, reason=why)(scope, receive, send)
            return
        response = JSONResponse({"detail": why}, status, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)


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
    app.state.blocks = HandBlocks(app.state.lockouts)  # end passes, and sessions (AccessGate)
    app.state.tasks = set()  # sharing lockouts and running the hook, in the background
    app.state.store = None
    app.state.rtc = RtcEcho(
        settings.ice_servers,
        settings.max_rtc_sessions,
        settings.rtc_session_timeout,
        enabled=settings.webrtc_enabled,
        port_range=settings.rtc_ports,
        voice=settings.monitor_voice,
        video=settings.monitor_video,
    )
    app.state.rtc.meter = app.state.traffic
    app.state.rtc.blocks = app.state.blocks

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
    app.add_middleware(NoStore)
    app.add_middleware(ForwardedPrefix, settings=settings)
    app.add_middleware(RequestLog)  # outermost: sees the request as it arrived

    for router in routers:
        app.include_router(router)

    index = STATIC / "index.html"
    logo = page.logo_file(settings)
    shell = page.Shell(settings, logo)
    origins = server_origins(settings)

    async def spa(request: Request) -> HTMLResponse:
        # Relative URLs in the page resolve against <base href>, which carries the prefix.
        # Visitors from outside internal_networks don't get the desk's tabs.
        ip, _ = client_address(request)
        outside = settings.internal_networks and not is_internal(ip, settings.internal_networks)
        hide = page.DESK_PAGES if outside else frozenset()
        root = request.scope.get("root_path", "")
        # Not the version for a visitor outside internal_networks, nor, without them, for
        # one who needs the code: it would tell an attacker which flaws to try. Without
        # internal_networks, a visitor who entered the code gets it from /api/v1/info.
        known = shows_version(request)
        body = shell.render(index.read_text(), root, __version__ if known else "", hide)
        policy = content_policy(body, origins)
        return HTMLResponse(
            body, headers={"Cache-Control": "no-cache", "Content-Security-Policy": policy}
        )

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
        spec = {**app.openapi(), "servers": [{"url": request.scope.get("root_path", "") or "/"}]}
        spec["info"] = {**spec["info"], "title": settings.brand_name}  # the explorer's heading
        if not shows_version(request):  # as the page has it: not for visitors who need a code
            spec["info"] = {**spec["info"], "version": ""}
        return JSONResponse(spec)

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request) -> HTMLResponse:
        # Swagger UI comes from our own assets (copied in by web/build.mjs), not a CDN, so
        # the explorer works on networks without internet access.
        root = request.scope.get("root_path", "")
        assets = f"{root}/assets/swagger"
        explorer = get_swagger_ui_html(
            openapi_url=root + "/api/openapi.json",
            title=escape(f"{settings.brand_name} API"),  # FastAPI writes it in as it is
            swagger_js_url=f"{assets}/swagger-ui-bundle.js?v={stamps.get('swagger-ui-bundle.js')}",
            swagger_css_url=f"{assets}/swagger-ui.css?v={stamps.get('swagger-ui.css')}",
            swagger_favicon_url=f"{root}/{shell.icon}",
        )
        explorer.headers["Content-Security-Policy"] = content_policy(
            bytes(explorer.body).decode(), []
        )
        return explorer

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
