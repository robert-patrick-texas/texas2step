"""The MAC address of the interface holding a local address, from ``ip -j address``."""

import json
import subprocess

import pytest

from texas2step import iface

# Trimmed `ip -j address show` output: loopback, a wired link, Wi-Fi with a secondary
# IPv4 address, and a WireGuard tunnel (no link-layer address).
LINKS = [
    {
        "ifname": "lo",
        "link_type": "loopback",
        "address": "00:00:00:00:00:00",
        "addr_info": [
            {"family": "inet", "local": "127.0.0.1"},
            {"family": "inet6", "local": "::1"},
        ],
    },
    {
        "ifname": "eth0",
        "link_type": "ether",
        "address": "00:11:22:33:44:55",
        "addr_info": [
            {"family": "inet", "local": "10.0.0.10"},
            {"family": "inet6", "local": "2001:db8:5::7c3a:91d2:5e10:c4b8"},
            {"family": "inet6", "local": "fe80::211:22ff:fe33:4455"},
        ],
    },
    {
        "ifname": "wlan0",
        "link_type": "ether",
        "address": "AA:BB:CC:DD:EE:FF",
        "addr_info": [
            {"family": "inet", "local": "192.168.1.20"},
            {"family": "inet", "local": "192.168.1.21"},
        ],
    },
    {"ifname": "wg0", "link_type": "none", "addr_info": [{"family": "inet", "local": "10.8.0.2"}]},
]


# `ip -j route get <server> from <local>`: the interface the kernel sends each one out of.
ROUTES = {
    ("198.51.100.20", "10.0.0.10"): "eth0",
    ("203.0.113.50", "10.0.0.10"): "wlan0",  # policy routing: eth0's address, out of Wi-Fi
    ("10.0.0.10", "10.0.0.10"): "lo",  # this computer's own address
    ("198.51.100.20", "10.8.0.2"): "wg0",
}


@pytest.fixture
def ip(monkeypatch):
    def run(args, **kwargs):
        if args[1:] == ["-j", "address", "show"]:
            rows = LINKS
        elif args[1:4] == ["-j", "route", "get"] and args[5] == "from":
            dev = ROUTES.get((args[4], args[6]))
            if dev is None:
                raise subprocess.CalledProcessError(2, args)  # "Error: Network is unreachable"
            rows = [{"dst": args[4], "from": args[6], "dev": dev}]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(iface.shutil, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(iface.subprocess, "run", run)


@pytest.mark.usefixtures("ip")
@pytest.mark.parametrize(
    ("local", "server", "mac"),
    [
        ("10.0.0.10", "198.51.100.20", "00:11:22:33:44:55"),
        ("10.0.0.10", "203.0.113.50", "aa:bb:cc:dd:ee:ff"),  # the NIC it leaves by
        ("10.0.0.10", "10.0.0.10", None),  # over loopback: no NIC
        ("10.8.0.2", "198.51.100.20", None),  # a tunnel has no MAC
        ("192.168.1.21", "198.51.100.99", "aa:bb:cc:dd:ee:ff"),  # no route told: the holder's
    ],
)
def test_mac_of_the_nic_a_connection_leaves_by(local, server, mac):
    assert iface.mac_of(local, server) == mac


@pytest.mark.parametrize(
    ("address", "mac"),
    [
        ("10.0.0.10", "00:11:22:33:44:55"),
        ("2001:db8:5::7c3a:91d2:5e10:c4b8", "00:11:22:33:44:55"),
        ("2001:0db8:0005:0000:7c3a:91d2:5e10:c4b8", "00:11:22:33:44:55"),  # spelled out
        ("fe80::211:22ff:fe33:4455%eth0", "00:11:22:33:44:55"),  # with a zone
        ("192.168.1.21", "aa:bb:cc:dd:ee:ff"),  # a secondary address, in lower case
        ("10.8.0.2", None),  # a tunnel has no MAC
        ("127.0.0.1", None),  # nor has loopback
        ("203.0.113.7", None),  # not this host's
        ("not an address", None),
    ],
)
@pytest.mark.usefixtures("ip")
def test_mac_of(address, mac):
    assert iface.mac_of(address) == mac


def test_no_ip_command(monkeypatch):
    # Not Linux, or no iproute2: no MAC, and no error.
    monkeypatch.setattr(iface.shutil, "which", lambda name: None)
    assert iface.mac_of("10.0.0.10") is None


def test_ip_command_fails(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(iface.shutil, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(iface.subprocess, "run", run)
    assert iface.mac_of("10.0.0.10") is None
