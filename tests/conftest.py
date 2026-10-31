from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from texas2step.app import create_app
from texas2step.config import Location, Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        server_id="t1",
        server_name="Test One",
        server_location="Testville",
        results_path=str(tmp_path / "results.db"),
        clients_file=str(tmp_path / "clients.db"),
        cpu_file=str(tmp_path / "cpu.db"),
        locations=[Location(id="peer", name="Peer", url="https://peer.example")],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture(scope="session")
def live_server(tmp_path_factory) -> Iterator[str]:
    """A real uvicorn server on loopback, for engine and WebRTC round trips."""
    port = _free_port()
    db = tmp_path_factory.mktemp("db")
    settings = Settings(
        results_path=str(db / "r.db"),
        clients_file=str(db / "c.db"),
        cpu_file=str(db / "cpu.db"),
        allow_any_target=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
