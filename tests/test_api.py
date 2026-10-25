import base64
import hashlib
import re
import time

from fastapi.testclient import TestClient

from texas2step import __version__
from texas2step.app import create_app
from texas2step.config import Link, Location, Settings


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
    assert set(info["capabilities"]) == {"websocket", "webrtc", "results", "measure", "stamps"}
    assert info["defaults"] == {"streams": 4, "seconds": 10.0, "packets": 500, "one_stream": True}

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


def test_diag_http_direct(client):
    r = client.get("/api/v1/diag/http")
    assert r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d["client"] == {"ip": "testclient", "port": 50000, "family": None}
    # Without a proxy, the socket's own end: TestClient's is named, not numbered.
    assert d["server"] == {"ip": None, "port": 80, "family": None}
    assert d["http_version"] == "HTTP/1.1"
    assert d["tls"] is None and d["tcp_rtt_ms"] is None and d["tcp_rttvar_ms"] is None
    assert abs(d["time_ms"] - time.time() * 1000) < 5000


NGINX_SAYS = {
    "x-forwarded-for": "2001:db8::7",
    "x-client-protocol": "HTTP/2.0",
    "x-client-port": "51234",
    "x-server-addr": "2001:db8::1",
    "x-server-port": "443",
    "x-tls-protocol": "TLSv1.3",
    "x-tls-cipher": "TLS_AES_256_GCM_SHA384",
    "x-tcp-rtt": "1234",
    "x-tcp-rttvar": "567",
}


def test_diag_http_through_a_trusted_proxy(client, settings):
    settings.trusted_proxies = ["testclient"]
    d = client.get("/api/v1/diag/http", headers=NGINX_SAYS).json()
    assert d["client"] == {"ip": "2001:db8::7", "port": 51234, "family": "IPv6"}
    assert d["server"] == {"ip": "2001:db8::1", "port": 443, "family": "IPv6"}
    assert d["http_version"] == "HTTP/2.0"
    assert d["tls"] == {"protocol": "TLSv1.3", "cipher": "TLS_AES_256_GCM_SHA384"}
    assert (d["tcp_rtt_ms"], d["tcp_rttvar_ms"]) == (1.234, 0.567)
    # nginx sends IPv4 listeners' addresses plainly; a mapped one is unwrapped.
    d = client.get("/api/v1/diag/http", headers={"x-server-addr": "::ffff:192.0.2.1"}).json()
    assert d["server"]["ip"] == "192.0.2.1" and d["server"]["family"] == "IPv4"


def test_diag_http_ignores_untrusted_and_junk_headers(client, settings):
    d = client.get("/api/v1/diag/http", headers=NGINX_SAYS).json()
    assert d["client"]["ip"] == "testclient" and d["client"]["port"] == 50000
    assert d["server"] == {"ip": None, "port": 80, "family": None}
    assert d["tls"] is None and d["tcp_rtt_ms"] is None and d["http_version"] == "HTTP/1.1"

    settings.trusted_proxies = ["testclient"]
    junk = {
        "x-forwarded-for": "192.0.2.9",
        "x-client-port": "99999",
        "x-server-addr": "not-an-ip",
        "x-server-port": "0",
        "x-tls-protocol": "TLSv1.3",
        "x-tls-cipher": "<script>alert(1)</script> " * 4,
        "x-tcp-rtt": "0",
        "x-tcp-rttvar": "lots",
    }
    d = client.get("/api/v1/diag/http", headers=junk).json()
    assert d["client"] == {"ip": "192.0.2.9", "port": None, "family": "IPv4"}
    assert d["server"] == {"ip": None, "port": None, "family": None}
    assert d["tls"] is None and d["tcp_rtt_ms"] is None and d["tcp_rttvar_ms"] is None


def test_diag_page_can_be_hidden(tmp_path):
    from texas2step.page import Shell

    template = (
        '<html lang="en"><base href="/" /><nav>'
        '<a href="monitor" data-link data-page="monitor">Monitor</a>'
        '<a href="diag" data-link data-page="diag">Diag</a></nav></html>'
    )
    settings = Settings(results_path=str(tmp_path / "r.db"), page_diag=False)
    page = Shell(settings, None).render(template, "", "0")
    assert 'data-page="diag"' not in page and 'data-page="monitor"' in page
    assert 'data-pages="test monitor history api"' in page
    assert 'data-page="diag"' in Shell(Settings(), None).render(template, "", "0")


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
        "client": {"ip": "203.0.113.7", "local_ip": "10.0.0.10"},
        "download": {"mbps": 940.5},
        "upload": {"mbps": 410.0},
        "latency": {"median": 3.2, "jitter": 0.4},
        "loss": {"loss_pct": 0.2},
    }
    saved = client.post("/api/v1/results", json=result)
    assert saved.status_code == 201
    rid = saved.json()["id"]
    assert saved.json()["download_mbps"] == 940.5
    assert saved.json()["client_local_ip"] == "10.0.0.10"
    assert saved.json()["client_mac"] is None

    full = client.get(f"/api/v1/results/{rid}").json()
    assert full["id"] == rid and full["upload"]["mbps"] == 410.0

    listing = client.get("/api/v1/results").json()["results"]
    assert listing[0]["id"] == rid
    assert listing[0]["latency_ms"] == 3.2
    assert listing[0]["loss_pct"] == 0.2
    assert listing[0]["client_local_ip"] == "10.0.0.10"


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


def test_measure_takes_a_listed_target_with_spaces_around_it(tmp_path):
    peer = Location("peer", "peer.example.net", "http://127.0.0.1:9")  # nothing listens there
    settings = Settings(results_path=str(tmp_path / "r.db"), locations=[peer])
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/v1/measure/latency", json={"target": " http://127.0.0.1:9 ", "count": 1})
    assert r.status_code == 502  # allowed, then unreachable; not 403


def test_rtc_offer_rejects_garbage(client):
    r = client.post("/api/v1/rtc/offer", json={"sdp": "not sdp", "type": "offer"})
    assert r.status_code == 400


def test_spa_routes(client):
    for path in ("/", "/monitor", "/diag", "/history", "/api", "/r/abc123"):
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


def test_api_answers_are_kept_in_no_browser_cache(client, settings):
    """Saved results hold callers' addresses: a browser that opened one keeps no copy."""
    saved = client.post("/api/v1/results", json={"version": 1})
    answers = [
        saved,
        client.get("/api/v1/results"),
        client.get(f"/api/v1/results/{saved.json()['id']}"),
        client.get("/api/v1/whoami"),
        client.get("/api/v1/info"),
    ]
    assert [r.headers["cache-control"] for r in answers] == ["no-store"] * len(answers)
    # Answers that say otherwise keep theirs: the probes' and transfers', and the page's.
    assert client.get("/api/v1/ping").headers["cache-control"].startswith("no-store, no-cache")
    assert client.get("/").headers["cache-control"] == "no-cache"
    # Behind nginx at a prefix too, passed through or stripped.
    settings.trusted_proxies = ["testclient"]
    prefix = {"x-forwarded-prefix": "/texas2step"}
    for path in ("/api/v1/results", "/texas2step/api/v1/results"):
        assert client.get(path, headers=prefix).headers["cache-control"] == "no-store"


def test_pages_say_which_scripts_and_servers_they_may_use(tmp_path):
    peer = Location(
        "dallas",
        "dallas.example.net",
        "https://dallas.example.net/texas2step",
        url_v4="https://v4.dallas.example.net:8443/texas2step",
    )
    settings = Settings(
        results_path=str(tmp_path / "r.db"),
        locations=[peer],
        server_url_v6="http://v6.houston.example.net/texas2step",
    )
    with TestClient(create_app(settings)) as c:
        r = c.get("/monitor")
        explorer = c.get("/api/docs")
    policy = dict(part.split(" ", 1) for part in r.headers["content-security-policy"].split("; "))
    # The page's one inline script (its theme, before the first paint), by its hash.
    inline = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    assert policy["script-src"] == f"'self' 'sha256-{digest}'"
    assert policy["connect-src"].split() == [
        "'self'",
        "http://v6.houston.example.net",
        "ws://v6.houston.example.net",
        "https://dallas.example.net",
        "wss://dallas.example.net",
        "https://v4.dallas.example.net:8443",
        "wss://v4.dallas.example.net:8443",
    ]
    assert policy["object-src"] == "'none'" and policy["base-uri"] == "'self'"
    # The API explorer's own inline script, by its hash too.
    assert "'sha256-" in explorer.headers["content-security-policy"]


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


def test_default_page_is_unbranded(client):
    page = client.get("/").text
    assert "<title>texas2step</title>" in page and "texas<em>2</em>step" in page
    assert 'class="bm-star"' in page
    for absent in (
        "data-default-theme",
        'class="notice"',
        "brand-org",
        "topbar-links",
        "Powered by",
        "data-pages",
        'href="api/docs"',  # the explorer is on the API tab, not in the footer
    ):
        assert absent not in page, absent
    for tab in ("test", "monitor", "history", "api"):
        assert f'data-page="{tab}"' in page
    assert 'id="footer-version" href="https://robert-patrick-texas.github.io/texas2step/"' in page


def test_brand_credit_is_a_link_text_or_nothing(tmp_path):
    def page(**kw):
        with TestClient(create_app(Settings(results_path=str(tmp_path / "r.db"), **kw))) as c:
            return c.get("/").text

    project = "https://robert-patrick-texas.github.io/texas2step/"
    assert f'<a id="footer-version" href="{project}"' in page()
    text = page(brand_credit="text", brand_name="NetCheck")
    assert '<span id="footer-version">texas2step</span>' in text
    assert project not in text and "Powered by" in text
    none = page(brand_credit="none", brand_name="NetCheck")
    assert 'id="footer-version"' not in none and "Powered by" not in none and project not in none
    assert "Measurements run in your browser" in none  # the rest of the footer stays


def test_hidden_pages_lose_their_tabs(tmp_path):
    settings = Settings(results_path=str(tmp_path / "r.db"), page_test=False, page_api=False)
    with TestClient(create_app(settings)) as c:
        page = c.get("/history").text
    assert '<html lang="en" data-pages="monitor diag history">' in page
    assert 'data-page="monitor"' in page and 'data-page="history"' in page
    assert 'data-page="test"' not in page and 'data-page="api"' not in page


def test_branding_is_written_into_the_page(tmp_path):
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>')
    settings = Settings(
        results_path=str(tmp_path / "r.db"),
        brand_name="Acme <Net> Check",
        brand_organization="Acme IT",
        brand_url="https://acme.example",
        brand_logo=str(logo),
        brand_notice="Authorized use only. <script>alert(1)</script>",
        brand_links=[
            Link("Service desk", "https://help.acme.example"),
            Link("Email", "mailto:noc@acme.example"),
        ],
        default_theme="texas",
    )
    with TestClient(create_app(settings)) as c:
        page = c.get("/history").text
        assert "<title>Acme &lt;Net&gt; Check</title>" in page
        assert '<span class="brand-word">Acme &lt;Net&gt; Check</span>' in page
        assert 'aria-label="Acme &lt;Net&gt; Check home"' in page
        assert 'data-default-theme="texas"' in page
        assert "Authorized use only. &lt;script&gt;alert(1)&lt;/script&gt;" in page
        assert "<script>alert(1)</script>" not in page
        org = '<a class="brand-org" href="https://acme.example" target="_blank" rel="noopener">'
        assert org + "Acme IT</a>" in page
        assert '<a href="mailto:noc@acme.example">Email</a>' in page
        assert "Powered by" in page and 'class="bm-star"' not in page
        src = re.search(r'<img class="brand-mark" src="([^"]+)"', page).group(1)
        r = c.get("/" + src)  # relative to <base href="/">
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in r.headers["content-security-policy"]


def test_own_footer_is_written_into_the_page(tmp_path):
    settings = Settings(
        results_path=str(tmp_path / "r.db"),
        brand_footer=(
            "© 2026 Acme <IT>.\nFor official use only.\n\n"
            "Report outages to the [NOC](tel:5550100) or the [desk](https://help.acme.example)."
        ),
        brand_footer_links=[
            Link("Privacy", "https://acme.example/privacy"),
            Link("Write to us", "mailto:it@acme.example"),
        ],
    )
    with TestClient(create_app(settings)) as c:
        page = c.get("/").text
    footer = re.search(r'<div class="own-footer">.*?</nav></div></div>', page, re.S).group(0)
    assert "<p>© 2026 Acme &lt;IT&gt;.<br />For official use only.</p>" in footer
    assert '<p>Report outages to the <a href="tel:5550100">NOC</a> or the ' in footer
    assert (
        '<a href="https://help.acme.example" target="_blank" rel="noopener">desk</a>.</p>' in footer
    )
    links = '<nav class="own-footer-links" aria-label="Footer links"><ul><li><a href="https://acme.example/privacy"'
    assert links in footer and '<li><a href="mailto:it@acme.example">Write to us</a></li>' in footer
    # Only links, or only text: one column.
    with TestClient(
        create_app(Settings(results_path=str(tmp_path / "r.db"), brand_footer="Hi"))
    ) as c:
        assert (
            '<div class="own-footer-body solo"><div class="own-footer-text"><p>Hi</p></div></div>'
            in c.get("/").text
        )
    with TestClient(create_app(Settings(results_path=str(tmp_path / "r.db")))) as c:
        assert "own-footer" not in c.get("/").text


def test_missing_logo_falls_back_to_the_star(tmp_path, caplog):
    settings = Settings(results_path=str(tmp_path / "r.db"), brand_logo=str(tmp_path / "x.svg"))
    with TestClient(create_app(settings)) as c:
        assert 'class="bm-star"' in c.get("/").text
        assert c.get("/brand/logo").status_code == 404
    assert "no such file" in caplog.text
