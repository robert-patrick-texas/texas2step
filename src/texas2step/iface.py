"""This host's network interfaces: the MAC address of the NIC a connection leaves by.

The CLI reports it with a result, so a network admin can find the client on a switch
port or wireless access point. The kernel says which interface it routes the connection
out of (``ip route get <server> from <local address>``), and that interface's MAC is
reported. It reads iproute2's ``ip`` (Linux); elsewhere, or for an interface without a
MAC (a tunnel, loopback), there is none.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from typing import Any

MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


def _ip(*args: str) -> list[dict[str, Any]]:
    """``ip -j <args>``, parsed; empty where it can't run or fails."""
    ip = shutil.which("ip") or next(
        (p for p in ("/usr/sbin/ip", "/sbin/ip") if shutil.which(p)), None
    )
    if not ip:
        return []
    try:
        out = subprocess.run(
            [ip, "-j", *args], capture_output=True, text=True, timeout=3, check=True
        )
        rows = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _address(text: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return None


def mac_of(local_ip: str, server_ip: str | None = None) -> str | None:
    """The MAC address of the interface a connection from ``local_ip`` to ``server_ip``
    leaves by, as the kernel routes it; without ``server_ip``, or where the route can't be
    told, of the interface that holds ``local_ip``. None for one without a MAC."""
    local = _address(local_ip)
    if local is None:
        return None
    links = _ip("address", "show")
    server = _address(server_ip)
    route = _ip("route", "get", str(server), "from", str(local)) if server else []
    dev = route[0].get("dev") if route else None
    if dev is None:
        dev = next(
            (
                link.get("ifname")
                for link in links
                if any(
                    _address(a.get("local")) == local
                    for a in link.get("addr_info") or []
                    if isinstance(a, dict)
                )
            ),
            None,
        )
    link = next((link for link in links if dev is not None and link.get("ifname") == dev), None)
    mac = str((link or {}).get("address") or "").lower()
    if (
        link
        and link.get("link_type") == "ether"
        and MAC.fullmatch(mac)
        and mac != "00:00:00:00:00:00"
    ):
        return mac
    return None
