"""Servers that defend together: signed lockouts, blocks and clears between servers."""

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from texas2step import access, cli, cluster
from texas2step.app import create_app
from texas2step.config import Location, Settings
from texas2step.lockouts import LockoutStore

PEER = {"X-Forwarded-For": "198.51.100.20"}  # dallas, as houston sees it
STRANGER = {"X-Forwarded-For": "203.0.113.9"}


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "access.key"
    access.write_key(path, access.new_key())
    return path


def settings(tmp_path, key_file, **kw) -> Settings:
    base: dict[str, Any] = {
        "server_id": "houston",
        "results_path": str(tmp_path / "r.db"),
        "trusted_proxies": ["testclient"],  # so X-Forwarded-For names the caller
        "access_code": "daily",
        "access_key_file": str(key_file),
        "access_phrases_file": str(tmp_path / "access-phrases.toml"),
        "access_lockouts_file": str(tmp_path / "access.db"),
        "internal_networks": ["10.0.0.0/8"],
        "access_share_lockouts": True,
        "access_peer_networks": ["198.51.100.20"],
        "locations": [Location(id="dallas", name="Dallas", url="https://dallas.example.net/t2s")],
    }
    return Settings(**(base | kw))


def request(key_file, body: dict, key: bytes | None = None, now: float | None = None):
    raw = json.dumps(body).encode()
    key = key or cluster.peer_key(access.read_key(key_file))
    return {"content": raw, "headers": {**PEER, **cluster.signed(key, raw, now)}}


def until(condition, seconds=5.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def test_signatures():
    key = cluster.peer_key(b"k" * 32)
    assert key != b"k" * 32 and len(key) == 32
    assert cluster.peer_key(b"k" * 32) == key  # every server works out the same one
    body, now = b'{"action":"block"}', 1_800_000_000.0
    headers = cluster.signed(key, body, now)
    assert cluster.verify(key, headers, body, now + 10) is None
    assert "signature" in str(cluster.verify(key, headers, body + b" ", now))
    assert "signature" in str(cluster.verify(cluster.peer_key(b"x" * 32), headers, body, now))
    assert "minutes off" in str(cluster.verify(key, headers, body, now + cluster.SKEW + 1))
    assert "minutes off" in str(cluster.verify(key, headers, body, now - cluster.SKEW - 1))
    assert "not a signed" in str(cluster.verify(key, {}, body, now))
    assert headers["x-texas2step-nonce"] != cluster.signed(key, body, now)["x-texas2step-nonce"]


def test_widest():
    from texas2step.lockouts import parse_network as net

    assert cluster.too_wide(net("203.0.113.0/24"), cluster.WIDEST) == ""
    assert cluster.too_wide(net("203.0.112.0/23"), cluster.WIDEST) == (
        "203.0.112.0/23 is wider than a /24"
    )
    assert cluster.too_wide(net("2001:db8:5::/48"), cluster.WIDEST) == ""
    assert cluster.too_wide(net("2001:db8::/47"), cluster.WIDEST)
    assert cluster.too_wide(net("128.0.0.0/1"), cluster.WIDEST_BY_HAND)


def test_a_signed_block_from_another_server(tmp_path, key_file):
    with TestClient(create_app(settings(tmp_path, key_file))) as c:
        block = {
            "from": "dallas",
            "to": "houston",
            "action": "block",
            "network": "203.0.113.0/24",
            "minutes": 45,
        }
        r = c.post("/api/v1/access/peer", **request(key_file, block))
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "shared by dallas from 198.51.100.20"
        # The network is locked out here now: the right code is refused too.
        code = c.app.state.codes.current(datetime.now(UTC)).code
        assert c.post("/api/v1/access", json={"code": code}, headers=STRANGER).status_code == 403
        # The same request again is refused: it was sent before.
        again = request(key_file, block)
        assert c.post("/api/v1/access/peer", **again).status_code == 200
        replay = c.post("/api/v1/access/peer", content=again["content"], headers=again["headers"])
        assert (replay.status_code, replay.json()["detail"]) == (403, "it was sent before")
        check = {"from": "dallas", "to": "houston", "action": "check"}
        assert c.post("/api/v1/access/peer", **request(key_file, check)).json()["ok"] is True
        clear = {"from": "dallas", "to": "houston", "action": "clear", "network": "all"}
        r = c.post("/api/v1/access/peer", **request(key_file, clear))
        assert r.json() == {"cleared": ["203.0.113.0/24"]}
        assert c.post("/api/v1/access", json={"code": code}, headers=STRANGER).status_code == 200


def test_passes_ended_by_another_server(tmp_path, key_file):
    with TestClient(create_app(settings(tmp_path, key_file))) as c:
        base = {"from": "dallas", "to": "houston", "action": "end-passes"}
        now = time.time()
        r = c.post("/api/v1/access/peer", **request(key_file, base | {"at": now}))
        assert r.status_code == 200 and r.json() == {"ended": now}
        lockouts = c.app.state.lockouts
        assert lockouts.passes_ended() == (now, "shared by dallas from 198.51.100.20")
        # An earlier end changes nothing; one far ahead of this clock is refused, as it
        # would stamp the passes given here that far ahead.
        assert c.post(
            "/api/v1/access/peer", **request(key_file, base | {"at": now - 60})
        ).json() == {"ended": now}
        r = c.post("/api/v1/access/peer", **request(key_file, base | {"at": now + 3600}))
        assert r.status_code == 400
        assert c.post("/api/v1/access/peer", **request(key_file, base)).status_code == 400
        assert lockouts.passes_ended()[0] == now
        assert not c.app.state.tasks  # not passed on


def test_what_another_server_cant_do(tmp_path, key_file):
    with TestClient(create_app(settings(tmp_path, key_file))) as c:

        def send(body, **kw):
            return c.post("/api/v1/access/peer", **request(key_file, body, **kw))

        block = {"from": "dallas", "to": "houston", "action": "block", "network": "203.0.113.0/24"}
        # From an address outside access_peer_networks: as if there were no such thing.
        stranger = request(key_file, block)
        stranger["headers"].update(STRANGER)
        assert c.post("/api/v1/access/peer", **stranger).status_code == 404
        # Another key, an old request, another server's request.
        r = send(block, key=cluster.peer_key(b"x" * 32))
        assert r.status_code == 403 and "different access key" in r.json()["detail"]
        assert send(block, now=time.time() - 600).status_code == 403
        r = send(block | {"to": "austin"})
        assert (r.status_code, r.json()["detail"]) == (403, "it is for austin, not houston")
        # Too wide, the desk's own networks, loopback.
        for network, why in (
            ("203.0.112.0/23", "wider than a /24"),
            ("10.1.0.0/24", "overlaps 10.0.0.0/8"),
            ("127.0.0.0/24", "overlaps 127.0.0.0/8"),
        ):
            r = send(block | {"network": network})
            assert r.status_code == 400 and why in r.json()["detail"], r.text
        assert send(block | {"minutes": 10081}).status_code == 400
        # Not passed on: a block from another server is never sent anywhere.
        assert not c.app.state.tasks
    # Sharing off: no such endpoint.
    with TestClient(create_app(settings(tmp_path, key_file, access_share_lockouts=False))) as c:
        assert c.post("/api/v1/access/peer", **request(key_file, block)).status_code == 404


def test_a_lockout_here_goes_to_the_others(tmp_path, key_file, monkeypatch):
    sent = []

    async def fake_send(settings, key, action, network, minutes=0):
        sent.append((action, network, minutes, key))
        return [("dallas", None)]

    monkeypatch.setattr(cluster, "send", fake_send)
    with TestClient(create_app(settings(tmp_path, key_file))) as c:
        for n in range(3):
            c.post("/api/v1/access", json={"code": f"wrong.code.{n}"}, headers=STRANGER)
        until(lambda: sent)
        assert sent == [("block", "203.0.113.0/24", 30.0, cluster.peer_key(c.app.state.codes.key))]
        # Another try within a third of the lockout: not sent again.
        c.post("/api/v1/access", json={"code": "wrong.code.4"}, headers=STRANGER)
        time.sleep(0.2)
        assert len(sent) == 1
        # Later, while the attacker keeps trying here, it goes again.
        store = c.app.state.lockouts
        assert store.due_to_share("203.0.113.0/24", 600, now=time.time() + 601)
        assert not store.due_to_share("203.0.113.0/24", 600, now=time.time() + 602)


def test_the_hook(tmp_path, key_file):
    out = tmp_path / "hook.out"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\necho "$@|$TEXAS2STEP_WHY|$TEXAS2STEP_SERVER" >> {out}\n')
    hook.chmod(0o755)
    with TestClient(create_app(settings(tmp_path, key_file, access_lockout_hook=str(hook)))) as c:
        c.app.state.settings.access_share_lockouts = False  # nothing to send in this test
        for n in range(3):
            c.post("/api/v1/access", json={"code": f"wrong.code.{n}"}, headers=STRANGER)
        until(lambda: out.exists())
        event, network, ends = out.read_text().split("|")[0].split(" ")
        assert (event, network) == ("lockout", "203.0.113.0/24")
        assert datetime.fromisoformat(ends) > datetime.now(UTC)
        assert out.read_text().strip().endswith("|3 wrong access codes in a row|houston")


def test_the_command_line(tmp_path, key_file, monkeypatch, capsys):
    sent = []

    async def fake_send(settings, key, action, network, minutes=0, kind="lockout", at=0):
        sent.append((action, network, minutes) + ((kind,) if action == "block" else ()))
        if action == "end-passes":
            sent[-1] = (action, at)
        return [("dallas", None), ("austin", "404: sharing is off there")]

    monkeypatch.setattr(cluster, "send", fake_send)
    config = tmp_path / "config.toml"
    config.write_text(
        f'server_id = "houston"\naccess_code = "daily"\naccess_key_file = "{key_file}"\n'
        f'access_lockouts_file = "{tmp_path / "access.db"}"\n'
        f'access_phrases_file = "{tmp_path / "p.toml"}"\naccess_share_lockouts = true\n'
        'access_peer_networks = ["198.51.100.20"]\n'
        '[[locations]]\nid = "dallas"\nname = "Dallas"\nurl = "https://dallas.example.net"\n'
    )
    cli.main(["access", "block", "203.0.113.7", "--minutes", "90", "--config", str(config)])
    out = capsys.readouterr().out
    assert "Other servers: sent to dallas; not to austin (404: sharing is off there)." in out
    cli.main(["access", "block", "203.0.112.0/22", "--config", str(config)])
    assert "Not sent to the other servers: 203.0.112.0/22 is wider than a /24" in (
        capsys.readouterr().out
    )
    with pytest.raises(SystemExit, match="wider than a /16"):
        cli.main(["access", "block", "203.0.0.0/15", "--config", str(config)])
    cli.main(["access", "lockouts", "--clear", "all", "--config", str(config)])
    # A block from here is set by hand, so it ends passes on the other servers too.
    assert sent == [("block", "203.0.113.7/32", 90.0, "block"), ("clear", "all", 0)]
    cli.main(["access", "passes", "--end", "--yes", "--config", str(config)])
    (action, at) = sent[-1]
    assert action == "end-passes" and abs(at - time.time()) < 5
    assert "Other servers: sent to dallas" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # austin doesn't take them
        cli.main(["access", "peers", "--config", str(config)])
    out = capsys.readouterr().out
    assert "dallas   takes them from this server" in out
    assert "austin   404: sharing is off there" in out


def test_settings():
    with pytest.raises(ValueError, match="needs access codes on"):
        Settings(access_share_lockouts=True, access_peer_networks=["198.51.100.20"])
    with pytest.raises(ValueError, match="access_peer_networks"):
        Settings(access_share_lockouts=True, access_code="daily")
    with pytest.raises(ValueError, match="full path"):
        Settings(access_lockout_hook="hook.sh")


def test_one_time_values_are_remembered_for_a_while(tmp_path):
    store = LockoutStore(tmp_path / "a.db")
    assert store.first_use("n1", 600, now=1000.0)
    assert not store.first_use("n1", 600, now=1500.0)
    assert store.first_use("n1", 600, now=1700.0)  # forgotten after 600 s
    assert os.path.exists(tmp_path / "a.db")


def test_a_block_set_by_hand_there_is_one_here(tmp_path, key_file):
    with TestClient(create_app(settings(tmp_path, key_file))) as c:
        base = {"from": "dallas", "to": "houston", "action": "block", "minutes": 45}
        r = c.post(
            "/api/v1/access/peer",
            **request(key_file, {**base, "network": "192.0.2.0/24", "kind": "block"}),
        )
        assert r.json()["by_hand"] is True
        assert r.json()["source"] == "shared by dallas from 198.51.100.20, set by hand"
        # A lockout there, or a block from a server that sends no kind: a lockout here.
        for net, extra in (("198.51.100.0/24", {"kind": "lockout"}), ("203.0.113.0/24", {})):
            r = c.post(
                "/api/v1/access/peer", **request(key_file, {**base, "network": net, **extra})
            )
            assert r.json()["by_hand"] is False, net
