"""The UDP probe's port range: server sockets stay inside it, sessions still connect, and
an exhausted range is refused rather than spilling onto other ports."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aioice import stun
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from texas2step.app import create_app
from texas2step.config import Settings
from texas2step.rtc import RtcBusy, RtcEcho

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
