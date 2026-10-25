"""The UDP probe's port range: server sockets stay inside it, sessions still connect, and
an exhausted range is refused rather than spilling onto other ports."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aioice import stun
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from texas2step import rtc
from texas2step.app import create_app
from texas2step.config import Settings
from texas2step.rtc import RtcBusy, RtcEcho, without_mdns

# Below Linux's default ephemeral range (32768-60999), so no OS-chosen port lands here.
RANGE = (29100, 29199)
CANDIDATE = re.compile(r"^a=candidate:\S+ \d+ udp \d+ \S+ (\d+) typ (\w+)", re.M)


def candidates(sdp: str) -> list[tuple[int, str]]:
    return [(int(port), kind) for port, kind in CANDIDATE.findall(sdp)]


def in_range(port: int, ports: tuple[int, int] = RANGE) -> bool:
    return ports[0] <= port <= ports[1]


@pytest.fixture
async def peers() -> AsyncIterator[list[RTCPeerConnection]]:
    made: list[RTCPeerConnection] = []
    yield made
    await asyncio.gather(*(pc.close() for pc in made))


async def offer(peers: list[RTCPeerConnection]) -> tuple[RTCPeerConnection, Any]:
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peers.append(pc)
    channel = pc.createDataChannel("probe", ordered=False, maxRetransmits=0)
    await pc.setLocalDescription(await pc.createOffer())
    return pc, channel


async def test_sessions_bind_inside_the_range_and_echo(peers):
    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=RANGE)
    echo.start()
    try:
        pc, channel = await offer(peers)
        answer = await echo.answer(pc.localDescription.sdp)
        server = candidates(answer["sdp"])
        assert server and all(in_range(port) for port, _ in server)
        # Connections made outside answer(), like this client, keep OS-chosen ports.
        assert not any(in_range(port) for port, _ in candidates(pc.localDescription.sdp))

        opened = asyncio.Event()
        echoed: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        channel.on("open", opened.set)
        channel.on("message", lambda m: echoed.done() or echoed.set_result(m))
        await pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        await asyncio.wait_for(opened.wait(), 10)
        channel.send(b"probe-1")
        assert await asyncio.wait_for(echoed, 5) == b"probe-1"
    finally:
        await echo.stop()


async def test_control_channel_reports_both_ends_of_the_udp_path(peers):
    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=RANGE)
    echo.start()
    try:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers.append(pc)
        pc.createDataChannel("probe", ordered=False, maxRetransmits=0)
        control = pc.createDataChannel("control")
        await pc.setLocalDescription(await pc.createOffer())
        answer = await echo.answer(pc.localDescription.sdp)

        opened = asyncio.Event()
        replies: asyncio.Queue[Any] = asyncio.Queue()
        control.on("open", opened.set)
        control.on("message", lambda m: replies.put_nowait(json.loads(m)))
        await pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        await asyncio.wait_for(opened.wait(), 10)
        control.send(json.dumps({"op": "addr"}))
        got = await asyncio.wait_for(replies.get(), 5)
        assert got["op"] == "addr"
        # The client end as the server sees it, and the server's own socket in the range.
        ipaddress.ip_address(got["ip"].split("%")[0])
        ipaddress.ip_address(got["server_ip"].split("%")[0])
        assert isinstance(got["port"], int) and in_range(got["server_port"])
    finally:
        await echo.stop()


async def test_stamps_on_request(peers):
    from texas2step.rtc import STAMP, STAMP_AT

    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=RANGE)
    echo.start()
    try:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers.append(pc)
        probe = pc.createDataChannel("probe", ordered=False, maxRetransmits=0)
        control = pc.createDataChannel("control")
        await pc.setLocalDescription(await pc.createOffer())
        answer = await echo.answer(pc.localDescription.sdp)

        opened = asyncio.Event()
        replies: asyncio.Queue[Any] = asyncio.Queue()
        echoes: asyncio.Queue[bytes] = asyncio.Queue()
        control.on("open", opened.set)
        control.on("message", lambda m: replies.put_nowait(json.loads(m)))
        probe.on("message", echoes.put_nowait)
        await pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        await asyncio.wait_for(opened.wait(), 10)

        body = bytes(range(40))
        probe.send(body)  # unchanged until asked
        assert await asyncio.wait_for(echoes.get(), 5) == body
        control.send(json.dumps({"op": "stamp"}))
        assert await asyncio.wait_for(replies.get(), 5) == {"op": "stamp"}
        probe.send(body)
        stamped = await asyncio.wait_for(echoes.get(), 5)
        # The client's own header and padding stay; the server's clock and count go between.
        assert len(stamped) == len(body)
        assert stamped[:STAMP_AT] == body[:STAMP_AT]
        assert stamped[STAMP_AT + STAMP.size :] == body[STAMP_AT + STAMP.size :]
        at, count = STAMP.unpack_from(stamped, STAMP_AT)
        assert count == 2 and at > 0
        probe.send(b"short")  # too short to carry a stamp: echoed as it came
        assert await asyncio.wait_for(echoes.get(), 5) == b"short"
    finally:
        await echo.stop()


class FakeMeter:
    """A traffic limit that counts outsiders, and is reached when `past` is set."""

    def __init__(self) -> None:
        self.past = False
        self.added: list[tuple[str, int, int]] = []

    def counted_for(self, kind: str) -> bool:
        return kind == "outside"

    def periods(self) -> tuple[None, None]:
        return None, None

    def refusal(self, *_: Any) -> object | None:
        return object() if self.past else None

    def notice(self) -> dict[str, Any]:
        return {
            "detail": "Testing temporarily disabled.",
            "message": "Testing temporarily disabled.",
            "url": None,
        }

    def add(self, kind: str, sent: int = 0, received: int = 0) -> None:
        self.added.append((kind, sent, received))


async def test_profiles_count_toward_a_traffic_limit(peers):
    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=RANGE, video=False)
    echo.meter = meter = FakeMeter()
    echo.start()
    try:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers.append(pc)
        probe = pc.createDataChannel("probe", ordered=False, maxRetransmits=0)
        control = pc.createDataChannel("control")
        await pc.setLocalDescription(await pc.createOffer())
        answer = await echo.answer(pc.localDescription.sdp, whose="outside")

        opened = asyncio.Event()
        replies: asyncio.Queue[Any] = asyncio.Queue()
        echoes: asyncio.Queue[bytes] = asyncio.Queue()
        control.on("open", opened.set)
        control.on("message", lambda m: replies.put_nowait(json.loads(m)))
        probe.on("message", echoes.put_nowait)
        await pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        await asyncio.wait_for(opened.wait(), 10)

        async def ask(name: str) -> dict[str, Any]:
            control.send(json.dumps({"op": "profile", "name": name}))
            return await asyncio.wait_for(replies.get(), 5)

        # Path probes, as before: echoed and not counted.
        probe.send(bytes(64))
        assert await asyncio.wait_for(echoes.get(), 5) == bytes(64)
        assert meter.added == []
        # This server withholds video; voice is counted both ways.
        assert (await ask("video"))["ok"] is False
        assert await ask("voice") == {"op": "profile", "ok": True, "name": "voice"}
        probe.send(bytes(200))
        await asyncio.wait_for(echoes.get(), 5)
        assert meter.added == [("outside", 200 + 230, 200 + 150)]  # with headers and acks
        # Past the limit: the client is told, and only path probes come back.
        meter.past = True
        await echo.hold()
        told = await asyncio.wait_for(replies.get(), 5)
        assert told["op"] == "limit" and told["message"] == "Testing temporarily disabled."
        probe.send(bytes(200))
        probe.send(bytes(64))
        assert await asyncio.wait_for(echoes.get(), 5) == bytes(64)
        assert echoes.empty()
        refused = await ask("voice")
        assert refused["ok"] is False and refused["reason"] == "traffic limit"
        assert len(meter.added) == 1
    finally:
        await echo.stop()


def test_addresses_are_null_before_a_path_exists():
    from texas2step.rtc import Session

    assert Session(id="x", pc=object()).addresses() == {
        "ip": None,
        "port": None,
        "server_ip": None,
        "server_port": None,
    }


async def test_exhausted_range_is_refused(peers):
    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=(29200, 29200))
    try:
        first, _ = await offer(peers)
        second, _ = await offer(peers)
        await echo.answer(first.localDescription.sdp)
        with pytest.raises(RtcBusy, match="29200-29200"):
            await echo.answer(second.localDescription.sdp)
        assert len(echo.sessions) == 1
    finally:
        await echo.stop()


class StunServer(asyncio.DatagramProtocol):
    """Answers binding requests with the sender's address, as a STUN server would."""

    def connection_made(self, transport: Any) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        request = stun.parse_message(data)
        response = stun.Message(stun.Method.BINDING, stun.Class.RESPONSE, request.transaction_id)
        response.attributes["XOR-MAPPED-ADDRESS"] = addr
        self.transport.sendto(bytes(response), addr)


async def test_stun_candidates_come_from_the_ranged_sockets(peers):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(StunServer, local_addr=("0.0.0.0", 0))
    stun_port = transport.get_extra_info("sockname")[1]
    echo = RtcEcho([f"stun:127.0.0.1:{stun_port}"], max_sessions=4, timeout=60, port_range=RANGE)
    try:
        pc, _ = await offer(peers)
        answer = await echo.answer(pc.localDescription.sdp)
        server = candidates(answer["sdp"])
        assert "srflx" in {kind for _, kind in server}
        assert all(in_range(port) for port, _ in server)
    finally:
        await echo.stop()
        transport.close()


def test_settings_reach_the_echo(tmp_path):
    settings = Settings(results_path=str(tmp_path / "r.db"), rtc_port_min=50000, rtc_port_max=50999)
    assert create_app(settings).state.rtc.port_range == (50000, 50999)
    assert create_app(Settings(results_path=str(tmp_path / "r.db"))).state.rtc.port_range is None


def test_offers_lose_their_mdns_candidates_only():
    offer = (
        "v=0\r\n"
        "a=candidate:1 1 udp 2122260223 1750fd45-5a12-4c1b-ab08-0b86318abc2c.local 41398"
        " typ host generation 0\r\n"
        "a=candidate:2 1 UDP 2122194687 192.0.2.10 50000 typ host\r\n"
        "a=candidate:3 1 udp 1686052607 203.0.113.9 61000 typ srflx raddr 0.0.0.0 rport 0\r\n"
        "a=candidate:4 1 udp 2122129151 2E34B8D9-578C-401A-8A52-E29978BDB7F9.LOCAL 46985"
        " typ host\r\n"
        "a=end-of-candidates\r\n"
    )
    got = without_mdns(offer)
    assert ".local" not in got.lower()
    assert "192.0.2.10 50000" in got and "203.0.113.9 61000" in got
    assert got.endswith("a=end-of-candidates\r\n")


def test_an_offer_of_mdns_candidates_only_keeps_ice_open():
    # Firefox: host candidates by name only, and end-of-candidates. Left in, the empty list
    # made aioice answer with no candidates at all.
    offer = (
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        "a=candidate:0 1 UDP 2122187007 1aa86bc4-5b66-435e-a15a-5e051c804bb9.local 52501"
        " typ host\r\n"
        "a=candidate:2 1 TCP 2105458943 1aa86bc4-5b66-435e-a15a-5e051c804bb9.local 9"
        " typ host tcptype active\r\n"
        "a=end-of-candidates\r\n"
        "a=setup:actpass\r\n"
    )
    got = without_mdns(offer)
    assert "a=candidate" not in got and "end-of-candidates" not in got
    assert got.endswith("a=setup:actpass\r\n")


async def test_an_offer_without_candidates_still_opens(peers):
    """Firefox's offer, once its .local names are out: no candidates, but end-of-candidates.
    The server answers with its own, learns the browser's address from its checks, and
    echoes."""
    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=RANGE)
    echo.start()
    try:
        pc, channel = await offer(peers)
        lines = [x for x in pc.localDescription.sdp.splitlines() if not x.startswith("a=candidate")]
        at = next(
            i
            for i, x in enumerate(lines)
            if x.startswith("a=end-of-candidates") or x.startswith("a=setup")
        )
        lines = [x for x in lines if not x.startswith("a=end-of-candidates")]
        sdp = "\r\n".join([*lines[:at], "a=end-of-candidates", *lines[at:]]) + "\r\n"
        answer = await echo.answer(sdp)
        assert candidates(answer["sdp"])

        opened = asyncio.Event()
        echoed: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        channel.on("open", opened.set)
        channel.on("message", lambda m: echoed.done() or echoed.set_result(m))
        await pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        await asyncio.wait_for(opened.wait(), 10)
        channel.send(b"probe-1")
        assert await asyncio.wait_for(echoed, 5) == b"probe-1"
    finally:
        await echo.stop()


async def test_a_block_set_by_hand_ends_open_sessions(peers, monkeypatch):
    covered: set[str] = set()

    class Blocks:
        async def cover(self, ip: str) -> bool:
            return ip in covered

    monkeypatch.setattr(rtc, "LIMIT_CHECK", 0.05)  # the reaper's round, every few seconds
    echo = RtcEcho([], max_sessions=4, timeout=60, port_range=RANGE)
    echo.blocks = Blocks()
    echo.start()
    try:
        for ip in ("203.0.113.9", "198.51.100.8"):
            pc, _ = await offer(peers)
            await echo.answer(pc.localDescription.sdp, ip=ip)
        unwatched, _ = await offer(peers)  # no access codes: not watched
        await echo.answer(unwatched.localDescription.sdp)
        covered.add("203.0.113.9")
        for _ in range(100):
            if len(echo.sessions) == 2:
                break
            await asyncio.sleep(0.02)
        assert sorted(str(s.ip) for s in echo.sessions.values()) == ["198.51.100.8", "None"]
    finally:
        await echo.stop()
