import re

from texas2step import __version__
from texas2step.app import create_app


def test_ping_headers(client):
    r = client.get("/api/v1/ping")
    assert r.status_code == 200
    assert r.json() == {"pong": True}
    assert r.headers["server-timing"].startswith("app;dur=")
    assert r.headers["timing-allow-origin"] == "*"
    assert "no-store" in r.headers["cache-control"]


def test_download_exact_size(client):
    r = client.get("/api/v1/down", params={"bytes": 3_000_000})
    assert r.status_code == 200
    assert len(r.content) == 3_000_000
    assert r.headers["content-length"] == "3000000"
    assert r.headers["content-type"] == "application/octet-stream"
    # Incompressible: not a run of one repeated byte.
    assert len(set(r.content[:4096])) > 200


def test_download_zero_and_cap(client, settings):
    assert client.get("/api/v1/down", params={"bytes": 0}).content == b""
    settings.max_download_bytes = 1000
    assert len(client.get("/api/v1/down", params={"bytes": 10**9}).content) == 1000
    assert client.get("/api/v1/down", params={"bytes": -1}).status_code == 422


def test_download_for_seconds(client):
    r = client.get("/api/v1/down", params={"seconds": 0.2})
    assert r.status_code == 200
    assert len(r.content) > 0
    assert "content-length" not in r.headers


def test_upload_counts_bytes(client):
    r = client.post("/api/v1/up", content=b"x" * 5_000_000)
    body = r.json()
    assert r.status_code == 200
    assert body["bytes"] == 5_000_000
    assert "rx;dur=" in r.headers["server-timing"]


def test_upload_limit(client, settings):
    settings.max_upload_bytes = 100
    assert client.post("/api/v1/up", content=b"x" * 1000).status_code == 413


def test_websocket_echo(client):
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_bytes(b"\x01\x02\x03")
        assert ws.receive_bytes() == b"\x01\x02\x03"
        ws.send_text("hello")
        assert ws.receive_text() == "hello"


def test_info_whoami_locations(client):
    info = client.get("/api/v1/info").json()
    assert info["version"] == __version__
    assert info["server"] == {"id": "t1", "name": "Test One", "location": "Testville"}
    assert set(info["capabilities"]) == {"websocket", "webrtc", "results", "measure"}

    me = client.get("/api/v1/whoami", headers={"user-agent": "pytest"}).json()
    assert me["user_agent"] == "pytest"
    assert me["http_version"] == "HTTP/1.1"

    ids = [loc["id"] for loc in client.get("/api/v1/locations").json()["locations"]]
    assert ids == ["t1", "peer"]


def test_forwarded_for_only_from_trusted_proxy(client, settings):
    spoof = {"x-forwarded-for": "203.0.113.9"}
    assert client.get("/api/v1/whoami", headers=spoof).json()["ip"] != "203.0.113.9"
    settings.trusted_proxies = ["testclient"]
    me = client.get("/api/v1/whoami", headers=spoof).json()
    assert me["ip"] == "203.0.113.9"
    assert me["family"] == "IPv4"
    assert me["via_proxy"] is True


def test_cors_preflight_for_upload(client):
    r = client.options(
        "/api/v1/up",
        headers={"origin": "https://other.example", "access-control-request-method": "POST"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] in {"*", "https://other.example"}


def test_results_round_trip(client):
    result = {
        "version": 1,
        "tool": f"texas2step-web/{__version__}",
        "server": {"id": "t1"},
        "download": {"mbps": 940.5},
        "upload": {"mbps": 410.0},
        "latency": {"median": 3.2, "jitter": 0.4},
        "loss": {"loss_pct": 0.2},
    }
    saved = client.post("/api/v1/results", json=result)
    assert saved.status_code == 201
    rid = saved.json()["id"]
    assert saved.json()["download_mbps"] == 940.5

    full = client.get(f"/api/v1/results/{rid}").json()
    assert full["id"] == rid and full["upload"]["mbps"] == 410.0

    listing = client.get("/api/v1/results").json()["results"]
    assert listing[0]["id"] == rid
    assert listing[0]["latency_ms"] == 3.2
    assert listing[0]["loss_pct"] == 0.2


def test_results_validation(client):
    assert client.post("/api/v1/results", json={"version": 2}).status_code == 422
    assert client.post("/api/v1/results", content=b"nope").status_code == 400
    assert client.get("/api/v1/results/missing").status_code == 404


def test_results_disabled(settings):
    from fastapi.testclient import TestClient

    from texas2step.app import create_app

    settings.results_enabled = False
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/v1/results").status_code == 404
        assert c.get("/api/v1/info").json()["capabilities"]["results"] is False


def test_measure_rejects_unlisted_target(client):
    r = client.post("/api/v1/measure/latency", json={"target": "http://169.254.169.254"})
    assert r.status_code == 403


def test_rtc_offer_rejects_garbage(client):
    r = client.post("/api/v1/rtc/offer", json={"sdp": "not sdp", "type": "offer"})
    assert r.status_code == 400


def test_spa_routes(client):
    for path in ("/", "/monitor", "/history", "/api", "/r/abc123"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
    assert client.get("/api/openapi.json").json()["info"]["title"] == "texas2step"


def test_path_prefix_from_trusted_proxy(client, settings):
    """Behind nginx at /texas2step/, the page, assets, API and docs all carry the prefix."""
    prefix = {"x-forwarded-prefix": "/texas2step"}
    assert '<base href="/" />' in client.get("/", headers=prefix).text  # untrusted: ignored
    settings.trusted_proxies = ["testclient"]
    for path in ("/", "/monitor", "/r/abc123"):
        assert '<base href="/texas2step/" />' in client.get(path, headers=prefix).text
    assert '<base href="/" />' in client.get("/").text  # direct access still works
    # nginx may strip the prefix (proxy_pass .../;) or pass it through.
    assert client.get("/assets/app.js", headers=prefix).status_code == 200
    assert client.get("/texas2step/assets/app.js", headers=prefix).status_code == 200
    assert client.get("/api/v1/ping", headers=prefix).json() == {"pong": True}
    assert "/texas2step/api/openapi.json" in client.get("/api/docs", headers=prefix).text
    with client.websocket_connect("/api/v1/ws", headers=prefix) as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi"


def test_path_prefix_must_be_a_plain_path(client, settings):
    settings.trusted_proxies = ["testclient"]
    for bad in ('/x"><script>alert(1)</script>', "texas2step", "/a b", "/../etc"):
        page = client.get("/", headers={"x-forwarded-prefix": bad}).text
        assert '<base href="/" />' in page, bad


def test_page_carries_the_version(client):
    # The UI reads its version from here; src/texas2step/__init__.py is the only source.
    assert f'<meta name="texas2step-version" content="{__version__}" />' in client.get("/").text


def test_api_explorer_is_self_hosted(client, settings):
    """/api/docs loads Swagger UI from this server, never a CDN, with or without a prefix."""
    settings.trusted_proxies = ["testclient"]
    for headers, root in (({}, ""), ({"x-forwarded-prefix": "/texas2step"}, "/texas2step")):
        page = client.get("/api/docs", headers=headers).text
        refs = re.findall(r"""(?:src|href|url)\s*[=:]\s*['"]([^'"]+)['"]""", page)
        assert refs and not [r for r in refs if "://" in r], refs
        assert f"{root}/api/openapi.json" in refs
        for ref in refs:
            # The test client has no proxy in front of it; the app accepts either form.
            r = client.get(ref, headers=headers)
            assert r.status_code == 200, ref
        assert any(r.endswith(".js") or ".js?" in r for r in refs)
        assert any(".css" in r for r in refs)


def test_openapi_servers_follow_each_request(client, settings):
    settings.trusted_proxies = ["testclient"]
    prefixed = client.get("/api/openapi.json", headers={"x-forwarded-prefix": "/texas2step"}).json()
    direct = client.get("/api/openapi.json").json()
    assert prefixed["servers"] == [{"url": "/texas2step"}]
    assert direct["servers"] == [{"url": "/"}]
    assert "/api/v1/ping" in direct["paths"] and direct["info"]["title"] == "texas2step"


async def test_upload_abandoned_by_the_client_is_not_an_error(settings):
    """Browsers cancel in-flight uploads when a timed test ends; that isn't a server error."""
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 65536, "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/up",
        "raw_path": b"/api/v1/up",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await create_app(settings)(scope, receive, send)  # raised ClientDisconnect before the fix
    assert sent[0]["status"] == 499
