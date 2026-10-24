"""Server side of the UDP probe: a WebRTC data-channel echo.

Browsers cannot send raw UDP, but a WebRTC data channel opened with
``ordered: false, maxRetransmits: 0`` is delivered over SCTP/DTLS/UDP without
retransmission, so a packet the network drops stays dropped. The client sends
numbered probes on the ``probe`` channel; we echo each one and keep receive/transmit
counters so the client can separate upstream from downstream loss by asking on the
reliable ``control`` channel. Echoes are unchanged unless the client asks for stamps:
then each carries the time it arrived here and how many probes had, so the client can
time each direction and place each loss in one.

By default the OS picks the UDP ports. Given a port range, every host socket of an
answered session is bound inside it instead, so a firewall can allow exactly that range.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import errno
import ipaddress
import json
import logging
import random
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

try:
    from aioice import Candidate, Connection
    from aioice.candidate import candidate_foundation, candidate_priority
    from aioice.ice import StunProtocol, server_reflexive_candidate
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
        rtcicetransport,
    )

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on installs without aiortc
    AVAILABLE = False


class RtcUnavailable(RuntimeError):
    pass


class RtcBusy(RuntimeError):
    pass


# The port range for ICE connections created while answering an offer (None: any port).
_PORT_RANGE: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "rtc_port_range", default=None
)
_RCVBUF = 256 * 1024  # the receive buffer aioice gives its own sockets
# With stamps on, an echo carries the server's clock when the probe arrived (ms) and the
# probes received so far, this one included, after the client's u32 sequence and f64 time.
STAMP = struct.Struct("<dI")
STAMP_AT = 12
# Monitor's profiles. Path probes are small and uncounted, as they always were. Voice and
# video sessions count toward a traffic limit; past it a session is held to path probes.
PROFILES = ("path", "voice", "video")
PATH_BYTES = 64
LIMIT_CHECK = 5.0  # seconds between checks that a counted session is still within a limit
# A traffic limit counts what the network carries: each probe's bytes, its SCTP, DTLS, UDP
# and IP headers, and the acknowledgements, which this server sends one of per probe. Per
# probe, measured over IPv6 for voice and video sessions (IPv4 carries a little less).
SENT_OVERHEAD = 230
RECEIVED_OVERHEAD = 150


def _bind_in_range(address: str, ports: tuple[int, int]) -> socket.socket | None:
    """A UDP socket bound to ``address`` on a free port in ``ports``, or None.

    Ports are tried from a random offset so concurrent sessions, and the worker processes
    sharing the range, rarely contend. A port in use is skipped; any other error means
    this address cannot be bound at all.
    """
    low, high = ports
    span = high - low + 1
    offset = random.randrange(span)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM)
    except OSError as exc:
        log.info("could not open a UDP socket for %s: %s", address, exc)
        return None
    try:
        for i in range(span):
            try:
                sock.bind((address, low + (offset + i) % span))
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
            else:
                return sock
        log.warning("UDP ports %d-%d are all in use on %s", low, high, address)
    except OSError as exc:
        log.info("could not bind %s in %d-%d: %s", address, low, high, exc)
    sock.close()
    return None


if AVAILABLE:

    class _PortRangeConnection(Connection):
        """An aioice ``Connection`` that can keep its host candidates in a port range.

        aioice binds every host socket to port 0, and aiortc has no option to change that.
        A connection created while ``_PORT_RANGE`` is set binds its host sockets here
        instead and leaves the STUN and TURN candidates to aioice. Without a range it
        behaves exactly like its base class.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.port_range = _PORT_RANGE.get()

        async def get_component_candidates(
            self,
            component: int,
            addresses: list[str],
            timeout: int = 5,  # noqa: ASYNC109 - the signature aioice calls
        ) -> list[Candidate]:
            if self.port_range is None:
                return await super().get_component_candidates(component, addresses, timeout)

            loop = asyncio.get_running_loop()
            hosts: list[tuple[StunProtocol, Candidate]] = []
            for address in addresses:
                sock = _bind_in_range(address, self.port_range)
                if sock is None:
                    continue
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RCVBUF)
                _, protocol = await loop.create_datagram_endpoint(
                    lambda: StunProtocol(self), sock=sock
                )
                host, port = sock.getsockname()[:2]
                candidate = Candidate(
                    foundation=candidate_foundation("host", "udp", host),
                    component=component,
                    transport="udp",
                    priority=candidate_priority(component, "host"),
                    host=host,
                    port=port,
                    type="host",
                )
                protocol.local_candidate = candidate
                hosts.append((protocol, candidate))
            if addresses and not hosts:
                low, high = self.port_range
                raise RtcBusy(f"no free UDP port in {low}-{high}")
            self._protocols += [protocol for protocol, _ in hosts]
            candidates = [candidate for _, candidate in hosts]

            # With no addresses, aioice still gathers a relayed (TURN) candidate, whose
            # socket only talks to the TURN server. Server-reflexive (STUN) candidates are
            # learned through the host sockets, so they are gathered here.
            relay = super().get_component_candidates(component, [], timeout)
            stun = self.stun_server
            reflexive = [
                asyncio.wait_for(server_reflexive_candidate(protocol, stun), timeout)
                for protocol, candidate in hosts
                if stun and ipaddress.ip_address(candidate.host).version == 4
            ]
            relayed, *found = await asyncio.gather(relay, *reflexive, return_exceptions=True)
            if isinstance(relayed, list):
                candidates += relayed
            candidates += [result[0] for result in found if isinstance(result, tuple)]
            return candidates


@dataclass
class Session:
    id: str
    pc: Any
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    rx: int = 0
    tx: int = 0
    rx_bytes: int = 0
    stamp: bool = False
    whose: str | None = None  # the client's kind for a traffic limit (traffic.TrafficMeter.kind)
    profile: str = "path"
    limited: bool = False  # held to path probes by a traffic limit
    control: Any = None  # the control channel, to tell the client of a limit reached

    def counters(self) -> dict[str, int]:
        return {"rx": self.rx, "tx": self.tx, "rx_bytes": self.rx_bytes}

    def addresses(self) -> dict[str, Any]:
        """Both ends of the UDP path ICE chose: the client as this server sees it (after
        any NAT), and this server's own address. Nulls until the path is up."""
        found: dict[str, Any] = {"ip": None, "port": None, "server_ip": None, "server_port": None}
        # aiortc has no public accessor for the selected pair; aioice keeps the nominated
        # pair per component. Guarded, so a change in either library gives nulls, not errors.
        try:
            pair = self.pc.sctp.transport.transport._connection._nominated.get(1)
            if pair is not None:
                found["ip"], found["port"] = pair.remote_addr
                found["server_ip"], found["server_port"] = pair.local_addr
        except Exception:
            pass
        return found


class RtcEcho:
    def __init__(
        self,
        ice_servers: list[str],
        max_sessions: int,
        timeout: float,
        enabled: bool = True,
        port_range: tuple[int, int] | None = None,
        video: bool = True,
    ) -> None:
        self.enabled = enabled and AVAILABLE
        self.video = video
        self.meter: Any = None  # a traffic.TrafficMeter, when the server has a traffic limit
        self._replies: set[asyncio.Future[None]] = set()
        self.ice_servers = ice_servers
        self.max_sessions = max_sessions
        self.timeout = timeout
        self.port_range = port_range
        self.sessions: dict[str, Session] = {}
        self._reaper: asyncio.Task[None] | None = None
        if self.enabled and port_range is not None:
            # aiortc builds its ICE connections from this module-level name. The subclass
            # changes nothing for connections created outside answer().
            rtcicetransport.Connection = _PortRangeConnection  # ty: ignore[invalid-assignment]

    def start(self) -> None:
        if self.enabled and self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        await asyncio.gather(*(self._close(s) for s in list(self.sessions.values())))

    def counted(self, session: Session) -> bool:
        """Whether a traffic limit counts this session's voice or video traffic."""
        return (
            self.meter is not None
            and session.whose is not None
            and self.meter.counted_for(session.whose)
        )

    def past_limit(self) -> dict[str, Any] | None:
        """The site's notice while a traffic limit is reached, as a refused test gets it;
        None within it. Reads the limit's file: call it off the event loop."""
        meter = self.meter
        return meter.notice() if meter is not None and meter.refusal(*meter.periods()) else None

    async def choose(self, session: Session, name: Any) -> dict[str, Any]:
        """The reply to {"op": "profile", "name": …}: whether the session may run it."""
        if name not in PROFILES:
            return {
                "op": "profile",
                "ok": False,
                "reason": f"expected one of {', '.join(PROFILES)}",
            }
        if name == "video" and not self.video:
            return {"op": "profile", "ok": False, "reason": "this server doesn't offer video"}
        counted = name != "path" and self.counted(session)
        if counted and (notice := await asyncio.to_thread(self.past_limit)):
            return {"op": "profile", "ok": False, "reason": "traffic limit", **notice}
        session.profile = name
        session.limited = False
        return {"op": "profile", "ok": True, "name": name}

    async def _reply(self, channel: Any, session: Session, name: Any) -> None:
        reply = await self.choose(session, name)
        with contextlib.suppress(Exception):
            channel.send(json.dumps(reply))

    async def answer(
        self, sdp: str, kind: str = "offer", whose: str | None = None
    ) -> dict[str, str]:
        if not self.enabled:
            raise RtcUnavailable("WebRTC probing is disabled or aiortc is not installed")
        if len(self.sessions) >= self.max_sessions:
            raise RtcBusy("too many concurrent probe sessions")

        # An explicit (possibly empty) list: given None, aiortc falls back to a public
        # STUN server, which an internal deployment should not contact unasked.
        config = RTCConfiguration(iceServers=[RTCIceServer(urls=url) for url in self.ice_servers])
        pc = RTCPeerConnection(configuration=config)
        session = Session(id=secrets.token_urlsafe(8), pc=pc, whose=whose)
        self.sessions[session.id] = session

        @pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if channel.label == "probe":

                @channel.on("message")
                def on_probe(message: bytes | str) -> None:
                    now = time.monotonic()
                    session.rx += 1
                    session.rx_bytes += len(message)
                    session.last_seen = now
                    counted = session.profile != "path"
                    if session.limited and len(message) > PATH_BYTES:
                        return  # past a traffic limit: only path probes are echoed
                    end = STAMP_AT + STAMP.size
                    if session.stamp and isinstance(message, bytes) and len(message) >= end:
                        stamp = STAMP.pack(now * 1000, session.rx & 0xFFFFFFFF)
                        message = message[:STAMP_AT] + stamp + message[end:]
                    channel.send(message)
                    session.tx += 1
                    if counted and self.meter is not None and session.whose is not None:
                        n = len(message)
                        self.meter.add(
                            session.whose, sent=n + SENT_OVERHEAD, received=n + RECEIVED_OVERHEAD
                        )

            elif channel.label == "control":
                session.control = channel

                @channel.on("message")
                def on_control(message: bytes | str) -> None:
                    session.last_seen = time.monotonic()
                    try:
                        request = json.loads(message)
                    except ValueError:
                        return
                    op = request.get("op")
                    if op == "stats":
                        channel.send(json.dumps({"op": "stats", **session.counters()}))
                    elif op == "addr":
                        channel.send(json.dumps({"op": "addr", **session.addresses()}))
                    elif op == "reset":
                        session.rx = session.tx = session.rx_bytes = 0
                        channel.send(json.dumps({"op": "reset"}))
                    elif op == "stamp":
                        session.stamp = True
                        channel.send(json.dumps({"op": "stamp"}))
                    elif op == "profile":
                        task = asyncio.ensure_future(
                            self._reply(channel, session, request.get("name"))
                        )
                        self._replies.add(task)
                        task.add_done_callback(self._replies.discard)

        @pc.on("connectionstatechange")
        async def on_state() -> None:
            if pc.connectionState in {"failed", "closed"}:
                await self._close(session)

        # The ICE connection is created by setRemoteDescription, in this task's context.
        token = _PORT_RANGE.set(self.port_range)
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
            await pc.setLocalDescription(await pc.createAnswer())
        except Exception:
            await self._close(session)
            raise
        finally:
            _PORT_RANGE.reset(token)
        return {"id": session.id, "sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    async def _close(self, session: Session) -> None:
        if self.sessions.pop(session.id, None) is not None:
            with contextlib.suppress(Exception):
                await session.pc.close()

    async def hold(self) -> None:
        """Past a traffic limit, hold the sessions it counts to path probes, and say why."""
        counted = [s for s in self.sessions.values() if s.profile != "path" and self.counted(s)]
        if not counted or not (notice := await asyncio.to_thread(self.past_limit)):
            return
        for session in counted:
            session.profile = "path"
            session.limited = True
            with contextlib.suppress(Exception):
                session.control.send(json.dumps({"op": "limit", **notice}))

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(LIMIT_CHECK)
            await self.hold()
            now = time.monotonic()
            for session in list(self.sessions.values()):
                idle = now - session.last_seen
                if idle > self.timeout or (session.rx == 0 and now - session.created > 30):
                    log.debug("closing idle rtc session %s", session.id)
                    await self._close(session)
