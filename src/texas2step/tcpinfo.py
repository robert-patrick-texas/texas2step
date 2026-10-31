"""The kernel's figures for one TCP connection, found by its two ends: how long it had data
in flight, how long the client's receive window held it back, and its round-trip time
(docs/methodology.md#when-the-device-is-the-limit).

Behind nginx the connection is nginx's to the client, which texas2step doesn't hold, so it
asks the kernel through NETLINK_SOCK_DIAG, as ``ss -ti`` does: any user may read any TCP
connection's tcp_info this way, and one lookup takes about 15 microseconds. Linux only;
elsewhere every lookup answers None.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLM_F_REQUEST = 1
NLMSG_ERROR = 2
INET_DIAG_INFO = 2
NOCOOKIE = 0xFFFFFFFF
# struct tcp_info (linux/tcp.h): the fields used, by offset. busy_time and the limited
# times came in Linux 4.10; a shorter answer has none of them.
RTT_AT, MIN_RTT_AT, BUSY_AT, NEEDED = 68, 148, 168, 192
NO_RTT = 0xFFFFFFFF  # tcpi_min_rtt before the connection's first RTT sample
# tcpi_state: a connection reset or closed by its client has its counters set back to 0
# by the kernel, so its figures say nothing of the transfer; nor does a listener.
TCP_CLOSE, TCP_LISTEN = 7, 10

INTERVAL = 0.25  # seconds between a transfer's lookups


@dataclass(frozen=True)
class Info:
    at: float  # time.monotonic() of the lookup
    busy_us: int  # microseconds the connection has had data in flight, since it opened
    rwnd_us: int  # of those, held back by the receiver's window
    sndbuf_us: int  # and by the sender's own send buffer
    rtt_us: int  # the smoothed round-trip time now
    min_rtt_us: int | None  # the lowest the kernel has seen on it; None before any


Ends = tuple[str, int, str, int]  # this end's address and port, then the client's
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class Diag:
    """One netlink socket per thread; lookups by a connection's two ends."""

    def __init__(self) -> None:
        self._local = threading.local()
        self.available = hasattr(socket, "AF_NETLINK")

    def _socket(self) -> socket.socket | None:
        sock = getattr(self._local, "sock", None)
        if sock is None and self.available:
            try:
                sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
                sock.settimeout(0.5)
            except OSError as exc:
                log.debug("tcpinfo: no NETLINK_SOCK_DIAG socket: %s", exc)
                self.available = False
                return None
            self._local.sock = sock
        return sock

    def lookup(self, ends: Ends) -> Info | None:
        """The connection's figures now, or None if the kernel has no such connection
        (closed, or the ends are wrong) or can't say."""
        local, lport, peer, pport = ends
        try:
            a, b = ipaddress.ip_address(local), ipaddress.ip_address(peer)
        except ValueError:
            return None
        tries: list[tuple[Address, Address]] = [(a, b)]
        # A dual-stack listener holds an IPv4 client on an IPv6 socket, its addresses
        # mapped (::ffff:192.0.2.1); a proxy may write them either way.
        if a.version == 4 and b.version == 4:
            tries.append(
                (ipaddress.IPv6Address(f"::ffff:{a}"), ipaddress.IPv6Address(f"::ffff:{b}"))
            )
        elif isinstance(a, ipaddress.IPv6Address) and isinstance(b, ipaddress.IPv6Address):
            if a.ipv4_mapped and b.ipv4_mapped:
                tries.append((a.ipv4_mapped, b.ipv4_mapped))
        elif a.version != b.version:
            return None
        for src, dst in tries:
            got = self._ask(src, lport, dst, pport)
            if got is not None:
                return got
        return None

    def _ask(self, src: Address, sport: int, dst: Address, dport: int) -> Info | None:
        sock = self._socket()
        if sock is None:
            return None
        family = socket.AF_INET if src.version == 4 else socket.AF_INET6
        sockid = (
            struct.pack("!HH", sport, dport)
            + src.packed.ljust(16, b"\0")
            + dst.packed.ljust(16, b"\0")
            + struct.pack("=III", 0, NOCOOKIE, NOCOOKIE)
        )
        req = struct.pack(
            "=BBBBI", family, socket.IPPROTO_TCP, 1 << (INET_DIAG_INFO - 1), 0, 0xFFFFFFFF
        )
        msg = req + sockid
        header = struct.pack("=IHHII", 16 + len(msg), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST, 1, 0)
        try:
            sock.send(header + msg)
            data = sock.recv(65536)
        except OSError as exc:
            log.debug("tcpinfo: lookup failed: %s", exc)
            return None
        at = time.monotonic()
        if len(data) < 16:
            return None
        length, kind = struct.unpack_from("=IH", data, 0)
        if kind != SOCK_DIAG_BY_FAMILY:
            return None  # NLMSG_ERROR: no such connection
        off = 16 + 72  # nlmsghdr, then struct inet_diag_msg
        end = min(length, len(data))
        while off + 4 <= end:
            alen, atype = struct.unpack_from("=HH", data, off)
            if alen < 4:
                break
            if atype == INET_DIAG_INFO:
                info = data[off + 4 : off + alen]
                if len(info) < NEEDED or info[0] in (TCP_CLOSE, TCP_LISTEN):
                    return None
                rtt = struct.unpack_from("=I", info, RTT_AT)[0]
                min_rtt = struct.unpack_from("=I", info, MIN_RTT_AT)[0]
                if min_rtt == NO_RTT:
                    min_rtt = None
                busy, rwnd, sndbuf = struct.unpack_from("=QQQ", info, BUSY_AT)
                return Info(at, busy, rwnd, sndbuf, rtt, min_rtt)
            off += (alen + 3) & ~3
        return None


DIAG = Diag()


@dataclass(frozen=True)
class Held:
    """What a transfer's lookups add up to: its seconds from the first lookup to the last,
    those with nothing in flight, those the receive window held back, and its RTTs."""

    seconds: float
    idle: float
    rwnd: float
    rtt_sum_ms: float
    rtt_n: int
    min_rtt_ms: float | None
    ready: float  # seconds the server had the next bytes ready and waited to hand them on


class Window:
    """Times how long the client held one download back: the connection's tcp_info when
    the first bytes go, every quarter second while they flow, and at the end. Held back is
    the time the client's TCP receive window limited what could be sent and, over HTTP/2
    (``multiplexed``), the time with nothing in flight: a client that reads too slowly
    stops granting the stream room, so the connection goes idle. Over HTTP/1.1 such a
    client fills its receive window instead, and nothing in flight means the server's
    side had nothing to send, which isn't the client's doing."""

    def __init__(self, ends: Ends | None, multiplexed: bool = True, diag: Diag = DIAG) -> None:
        self.ends = ends if diag.available else None
        self.multiplexed = multiplexed
        self.diag = diag
        self.first: Info | None = None
        self.last: Info | None = None
        self.rtts: list[int] = []
        self._next = 0.0
        # Seconds the server waited to hand bytes on (backpressure from nginx or the
        # connection). An app that can't produce bytes fast enough waits little, and its
        # connection goes idle for its own sake, not the client's.
        self.waited = 0.0

    def moved(self) -> None:
        if self.ends is None:
            return
        now = time.monotonic()
        if now >= self._next:
            self._next = now + INTERVAL
            self._take()

    def _take(self) -> None:
        assert self.ends is not None
        got = self.diag.lookup(self.ends)
        if got is None:
            if self.first is None:
                self.ends = None  # not a connection the kernel knows: stop asking
            return
        before = self.last or self.first
        if before is not None and got.busy_us < before.busy_us:
            self.ends = None  # its counters went back: another connection, or a reset one
            return
        if self.first is None:
            self.first = got
        else:
            self.last = got
        if got.rtt_us:
            self.rtts.append(got.rtt_us)

    def held(self) -> Held | None:
        """The figures, after one last lookup; None without two lookups."""
        if self.ends is not None and self.first is not None:
            self._take()  # a connection the client closed keeps the figures it had
        first, last = self.first, self.last
        if first is None or last is None or last.at <= first.at:
            return None
        seconds = last.at - first.at
        busy = max(0, last.busy_us - first.busy_us) / 1e6
        rwnd = max(0, last.rwnd_us - first.rwnd_us) / 1e6
        return Held(
            seconds=seconds,
            idle=max(0.0, seconds - busy) if self.multiplexed else 0.0,
            rwnd=min(rwnd, seconds),
            rtt_sum_ms=sum(self.rtts) / 1000,
            rtt_n=len(self.rtts),
            min_rtt_ms=last.min_rtt_us / 1000 if last.min_rtt_us is not None else None,
            ready=min(self.waited, seconds),
        )
