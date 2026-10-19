import logging
import os
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from websockets.sync.client import connect

from texas2step.app import RequestLog, create_app

NGINX = {"x-forwarded-for": "2001:db8::7", "x-client-protocol": "HTTP/2.0"}


def lines(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "texas2step.access"]


def test_request_lines_name_the_client(client, settings, caplog):
    caplog.set_level(logging.INFO, logger="texas2step.access")
    settings.trusted_proxies = ["testclient"]
    settings.max_upload_bytes = 10
    client.get("/api/v1/diag/http?n=7", headers=NGINX)
    client.get("/api/v1/nope%0Aforged", headers=NGINX)
    client.post("/api/v1/up", content=b"x" * 100, headers=NGINX)
    with client.websocket_connect("/api/v1/ws", headers=NGINX):
        pass
    assert lines(caplog) == [
        ("INFO", '2001:db8::7 - "GET /api/v1/diag/http?n=7 HTTP/2.0" 200'),
        ("WARNING", '2001:db8::7 - "GET /api/v1/nope%0Aforged HTTP/2.0" 404'),
        ("WARNING", '2001:db8::7 - "POST /api/v1/up HTTP/2.0" 413'),
        ("INFO", '2001:db8::7 - "WebSocket /api/v1/ws" 101'),
    ]
    # Untrusted peers can't name themselves.
    settings.trusted_proxies = []
    caplog.clear()
    client.get("/api/v1/ping", headers=NGINX)
    assert lines(caplog) == [("INFO", 'testclient - "GET /api/v1/ping HTTP/1.1" 200')]


def test_an_abandoned_upload_is_not_a_failure(settings, caplog):
    caplog.set_level(logging.INFO, logger="texas2step.access")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/up",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "http_version": "1.1",
        "app": create_app(settings),
    }
    RequestLog._line(scope, 499)
    assert lines(caplog) == [("INFO", '127.0.0.1 - "POST /api/v1/up HTTP/1.1" 499')]


def test_an_unhandled_error_logs_500(settings, caplog):
    app = create_app(settings)

    async def boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/boom", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/boom").status_code == 500
    assert lines(caplog) == [("WARNING", 'testclient - "GET /boom HTTP/1.1" 500')]


@pytest.mark.parametrize("debug", [False, True])
def test_serve_logs_failures_and_with_debug_everything(tmp_path, free_port, debug):
    config = tmp_path / "config.toml"
    config.write_text(f'results_path = "{tmp_path / "r.db"}"\ndebug = {str(debug).lower()}\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("TEXAS2STEP_")}
    serve = [sys.executable, "-m", "texas2step", "serve", "--host", "127.0.0.1"]
    proc = subprocess.Popen(
        [*serve, "--port", str(free_port), "--config", str(config)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{free_port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                httpx.get(base + "/api/v1/health", timeout=1)
                break
            except httpx.TransportError:
                assert proc.poll() is None and time.monotonic() < deadline, "server didn't start"
                time.sleep(0.1)
        # From 127.0.0.1, a trusted proxy by default, as nginx would be.
        who = {"x-forwarded-for": "203.0.113.7"}
        assert httpx.get(base + "/api/v1/ping", headers=who).status_code == 200
        assert httpx.get(base + "/api/v1/nope", headers=who).status_code == 404
        with connect(f"ws://127.0.0.1:{free_port}/api/v1/ws", additional_headers=who) as ws:
            ws.send("hi")
            assert ws.recv() == "hi"
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=15)
    assert out == ""
    assert "Application startup complete" in err
    requests = [line for line in err.splitlines() if " - " in line]
    want = ['WARNING: 203.0.113.7 - "GET /api/v1/nope HTTP/1.1" 404']
    if debug:
        want = [
            'INFO: 127.0.0.1 - "GET /api/v1/health HTTP/1.1" 200',
            'INFO: 203.0.113.7 - "GET /api/v1/ping HTTP/1.1" 200',
            *want,
            'INFO: 203.0.113.7 - "WebSocket /api/v1/ws" 101',
        ]
    # uvicorn's own request and WebSocket lines, with the proxy's address, are gone.
    assert [" ".join(line.split(maxsplit=1)) for line in requests] == want
    assert "connection open" not in err and "connection closed" not in err


LIBRARIES = """
import logging, logging.config, sys
import aioice, aiortc  # their NullHandlers, as in the server
from texas2step import logs
logging.config.dictConfig(logs.config(sys.argv[1] == "debug"))
for name, level in [
    ("texas2step.page", "warning"), ("texas2step.rtc", "info"), ("texas2step.rtc", "debug"),
    ("aioice.ice", "warning"), ("aioice.ice", "info"), ("aioice.ice", "debug"),
    ("aiortc.rtcdtlstransport", "debug"), ("aiortc.rtcsctptransport", "debug"),
]:
    getattr(logging.getLogger(name), level)("%s %s", name, level)
"""


@pytest.mark.parametrize(
    ("mode", "shown"),
    [
        # The libraries' warnings used to be dropped: they had a NullHandler and nothing else.
        (
            "quiet",
            [
                "WARNING: texas2step.page: texas2step.page warning",
                "WARNING: aioice.ice: aioice.ice warning",
            ],
        ),
        (
            "debug",
            [
                "WARNING: texas2step.page: texas2step.page warning",
                "INFO: texas2step.rtc: texas2step.rtc info",
                "DEBUG: texas2step.rtc: texas2step.rtc debug",
                "WARNING: aioice.ice: aioice.ice warning",
                "INFO: aioice.ice: aioice.ice info",
                # STUN and SCTP log every packet at DEBUG: left out even here.
                "DEBUG: aiortc.rtcdtlstransport: aiortc.rtcdtlstransport debug",
            ],
        ),
    ],
)
def test_texas2step_and_webrtc_messages(mode, shown):
    run = subprocess.run(
        [sys.executable, "-c", LIBRARIES, mode], capture_output=True, text=True, check=True
    )
    assert [" ".join(line.split(maxsplit=1)) for line in run.stderr.splitlines()] == shown
