#!/usr/bin/env python3
"""texas2step setup: check this host, then write an install script made for it.

Copy this file and the texas2step .deb it was built with to the server, side by side,
and run it there:

    sudo ./texas2step-setup --id houston --name "houston.example.net" --location "Houston, TX" \\
        --peer https://www.example.net/texas2step

It changes nothing. It checks what texas2step needs on this host, then writes two files
to the current directory (or --out):

- texas2step-preflight.txt: what is fine, what the install will change, what you must
  fix first, and notes. It is printed too.
- texas2step-install.sh: the install, reinstall or upgrade for this host, to read and
  then run with sudo. It isn't written while anything must be fixed first.

Standard library only, so it runs on a base Ubuntu before texas2step is installed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Written in by packaging/build-deb.sh: the package this script was built with.
DEB_NAME = "@DEB_NAME@"
DEB_SHA256 = "@DEB_SHA256@"

WANT_OS = ("ubuntu", "26.04")
CONFIG = "/etc/texas2step/config.toml"
DEFAULTS = "/etc/default/texas2step"
NGINX_PREFIX = "/etc/nginx"
UPSTREAM_CONF = "/etc/nginx/conf.d/texas2step.conf"
SNIPPET = "snippets/texas2step.conf"
SPEEDTEST_SNIPPET = "snippets/texas2step-speedtest.conf"
DEFAULT_PORTS = (61000, 61999)
REPORT = "texas2step-preflight.txt"
INSTALL = "texas2step-install.sh"
MARK = "# Written by texas2step-setup"

OK, CHANGE, FIX, NOTE = "ok", "change", "fix", "note"
SECTIONS = {OK: "OK", CHANGE: "Will change", FIX: "You must fix", NOTE: "Note"}
VERBS = {
    "fresh": "install",
    "reinstall": "reinstall",
    "upgrade": "upgrade to",
    "downgrade": "downgrade to",
}


@dataclass
class Finding:
    level: str
    text: str


# ---- nginx: parse `nginx -T` ---------------------------------------------------------


@dataclass
class Directive:
    name: str
    args: list[str]
    line: int  # first line, 1-based
    end: int  # the line of its ";" or "{"
    block: list[Directive] | None = None


def _tokens(text: str) -> Iterator[tuple[str, int, bool]]:
    """(token, line, quoted) for nginx config text; comments dropped."""
    i, line, n = 0, 1, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{};":
            yield c, line, False
            i += 1
        elif c in "\"'":
            start, j, buf = line, i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n and text[j + 1] in (c, "\\"):
                    j += 1  # an escaped quote or backslash; nginx keeps others, as in "\."
                if text[j] == "\n":
                    line += 1
                buf.append(text[j])
                j += 1
            yield "".join(buf), start, True
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "{};":
                j += 1
            yield text[i:j], line, False
            i = j


def parse_conf(text: str) -> list[Directive]:
    """nginx directives as a tree. Forgiving: it reads configs nginx already accepted."""
    root: list[Directive] = []
    stack: list[list[Directive]] = [root]
    words: list[str] = []
    first = 0
    for tok, line, quoted in _tokens(text):
        if quoted or tok not in "{};":
            if not words:
                first = line
            words.append(tok)
        elif tok == ";" and words:
            stack[-1].append(Directive(words[0], words[1:], first, line))
            words = []
        elif tok == "{":
            block: list[Directive] = []
            stack[-1].append(
                Directive(words[0] if words else "", words[1:], first or line, line, block)
            )
            stack.append(block)
            words = []
        elif tok == "}" and len(stack) > 1:
            stack.pop()
            words = []
    return root


def split_dump(text: str) -> dict[str, str]:
    """`nginx -T` output: each configuration file's path and text."""
    files: dict[str, str] = {}
    path, lines = None, []
    for line in text.splitlines():
        m = re.match(r"^# configuration file (.+):$", line)
        if m:
            if path:
                files[path] = "\n".join(lines) + "\n"
            path, lines = m.group(1), []
        elif path:
            lines.append(line)
    if path:
        files[path] = "\n".join(lines) + "\n"
    return files


@dataclass
class Server:
    file: str
    line: int
    names: list[str]
    https: bool
    port: int  # the HTTPS port, or the first listen port
    addr: str  # an address it listens on, for checking it locally
    insert_after: int  # the line the include goes after (end of server_name)
    indent: str
    locations: list[str] = field(default_factory=list)  # paths, including from includes
    published: list[str] = field(default_factory=list)  # locations proxied to texas2step

    def label(self) -> str:
        names = " ".join(self.names) or "(no server_name)"
        return f"{names} in {self.file}:{self.line}"


def _listen_port(args: list[str]) -> int:
    addr = args[0] if args else "80"
    m = re.search(r"(\d+)$", addr.rsplit("]", 1)[-1])
    return int(m.group(1)) if m and (":" in addr or addr.isdigit()) else 80


def _listen_addr(args: list[str]) -> str:
    """An IPv4 address the listen directive binds, or 127.0.0.1 for a wildcard."""
    host = args[0].rpartition(":")[0] if args and ":" in args[0] else ""
    return host if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) else "127.0.0.1"


def _resolve(pattern: str, files: dict[str, str]) -> list[str]:
    full = pattern if pattern.startswith("/") else os.path.join(NGINX_PREFIX, pattern)
    return [f for f in files if fnmatch.fnmatch(f, full)]


def _location_path(args: list[str]) -> str:
    return args[-1] if args else ""


def _walk_locations(
    block: list[Directive], files: dict[str, str], trees: dict[str, list[Directive]], seen: set
) -> Iterator[tuple[str, bool]]:
    """(location path, proxies to texas2step) in a block, following includes."""
    for d in block:
        if d.name == "location" and d.block is not None:
            proxied = any(
                c.name == "proxy_pass" and c.args and c.args[0].startswith("http://texas2step")
                for c in d.block
            )
            yield _location_path(d.args), proxied
            yield from _walk_locations(d.block, files, trees, seen)
        elif d.name == "include" and d.args:
            for f in _resolve(d.args[0], files):
                if f not in seen:
                    yield from _walk_locations(trees[f], files, trees, seen | {f})


def servers(files: dict[str, str]) -> list[Server]:
    """Every http server { } block nginx loads."""
    trees = {f: parse_conf(t) for f, t in files.items()}
    lines = {f: t.splitlines() for f, t in files.items()}
    found: list[Server] = []

    def visit(block: list[Directive], f: str, parent: str) -> None:
        for d in block:
            if d.block is None:
                continue
            if d.name == "server" and parent in {"http", ""}:
                listens = [c.args for c in d.block if c.name == "listen"]
                https = any("ssl" in a or "quic" in a or _listen_port(a) == 443 for a in listens)
                mine = [
                    a
                    for a in listens
                    if not https or "ssl" in a or "quic" in a or _listen_port(a) == 443
                ]
                names = [a for c in d.block if c.name == "server_name" for a in c.args]
                anchor = next((c for c in d.block if c.name == "server_name"), None)
                anchor = anchor or next((c for c in reversed(d.block) if c.name == "listen"), None)
                after = anchor.end if anchor else d.end
                text = lines[f][(anchor.line if anchor else d.line + 1) - 1]
                indent = text[: len(text) - len(text.lstrip())] if anchor else "    "
                port = _listen_port(mine[0]) if mine else 80
                addr = _listen_addr(mine[0]) if mine else "127.0.0.1"
                server = Server(f, d.line, names, https, port, addr, after, indent)
                for path, proxied in _walk_locations(d.block, files, trees, {f}):
                    server.locations.append(path)
                    if proxied:
                        server.published.append(path)
                found.append(server)
            elif d.name not in {"stream", "mail", "upstream"}:
                visit(d.block, f, d.name)

    for f, tree in trees.items():
        visit(tree, f, "")
    return found


def loads_upstream(files: dict[str, str]) -> bool:
    """Whether nginx includes /etc/nginx/conf.d/*.conf, where the package puts its upstream."""
    for tree in (parse_conf(t) for t in files.values()):
        stack = list(tree)
        while stack:
            d = stack.pop()
            if d.name == "include" and d.args:
                full = d.args[0] if d.args[0].startswith("/") else f"{NGINX_PREFIX}/{d.args[0]}"
                if fnmatch.fnmatch(UPSTREAM_CONF, full):
                    return True
            stack.extend(d.block or [])
    return False


# ---- Firewalls ----------------------------------------------------------------------


def covers(spec: str, lo: int, hi: int) -> bool:
    """Whether a port list such as "22,61000:61999" or "61000-61999" holds lo..hi."""
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:[:-]\s*(\d+))?\s*", part)
        if m and int(m.group(1)) <= lo and hi <= int(m.group(2) or m.group(1)):
            return True
    return False


def ufw_allows(status: str, lo: int, hi: int) -> bool:
    """`ufw status`: is there an inbound ALLOW for UDP lo..hi?"""
    for line in status.splitlines():
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 3 or not cols[1].startswith("ALLOW") or cols[1].endswith("OUT"):
            continue
        to = cols[0].replace(" (v6)", "")
        if to.startswith("Anywhere"):
            return True
        ports, _, proto = to.partition("/")
        if proto in {"", "udp"} and covers(ports, lo, hi):
            return True
    return False


def firewalld_allows(ports: str, lo: int, hi: int) -> bool:
    """`firewall-cmd --list-ports`, e.g. "22/tcp 61000-61999/udp"."""
    return any(
        covers(spec, lo, hi)
        for spec, _, proto in (item.partition("/") for item in ports.split())
        if proto == "udp"
    )


def iptables_state(rules: str, lo: int, hi: int) -> tuple[bool, bool]:
    """`iptables -S INPUT`: (drops what isn't allowed, allows UDP lo..hi)."""
    filtering = allowed = False
    for line in rules.splitlines():
        words = line.split()
        if words[:3] in (["-P", "INPUT", "DROP"], ["-P", "INPUT", "REJECT"]):
            filtering = True
        if words[:2] != ["-A", "INPUT"] or "-j" not in words:
            continue
        target = words[words.index("-j") + 1]
        conditions = [w for w in words[2:] if w.startswith("-") and w not in {"-j", "-m"}]
        if target in {"DROP", "REJECT"} and not set(conditions) - {"--reject-with"}:
            filtering = True
        if target == "ACCEPT" and "-i" not in words:
            if not conditions:
                allowed = True
            for flag in ("--dport", "--dports"):
                if flag in words and "udp" in words:
                    spec = words[words.index(flag) + 1]
                    allowed = allowed or covers(spec, lo, hi)
    return filtering, allowed


def nft_state(ruleset: str, lo: int, hi: int) -> tuple[bool, bool]:
    """`nft list ruleset`: (an input hook drops by default, a rule accepts UDP lo..hi)."""
    filtering = bool(re.search(r"hook input[^;]*;[^;]*policy drop", ruleset))
    allowed = False
    for m in re.finditer(r"udp dport (\{[^}]*\}|\S+)[^\n]*\baccept\b", ruleset):
        allowed = allowed or covers(m.group(1).strip("{} ").replace(" ", ""), lo, hi)
    return filtering, allowed


# ---- What we know about the host ------------------------------------------------------


@dataclass
class Dep:
    name: str
    need: str  # e.g. ">= 3.14", or ""
    installed: str | None
    ok: bool  # installed and satisfies need
    candidate_ok: bool  # apt can install a version that satisfies need


@dataclass
class Peer:
    url: str
    info: dict | None = None
    error: str = ""


@dataclass
class Host:
    hostname: str = "localhost"
    os_release: dict[str, str] = field(default_factory=dict)
    arch: str = "amd64"
    deb_path: str = ""
    deb_exists: bool = True
    deb_sha256: str = ""
    deb: dict[str, str] = field(default_factory=dict)  # control fields
    deps: list[Dep] = field(default_factory=list)
    systemd: bool = True
    free_kib: int = 10 << 20
    nginx_installed: bool = True
    nginx_active: bool = True
    nginx_dump: str = ""  # nginx -T
    nginx_error: str = ""  # when nginx -T fails
    site_texts: dict[str, str] = field(default_factory=dict)  # real path -> text on disk
    port_users: list[str] = field(default_factory=list)  # listeners that aren't texas2step
    installed: str | None = None  # installed texas2step version
    version_cmp: str = ""  # installed vs package: lt, eq or gt
    service_active: bool = False
    config: str | None = None  # /etc/texas2step/config.toml
    defaults: dict[str, str] = field(default_factory=dict)  # /etc/default/texas2step
    # md5 of the settings file now, of the template the installed version shipped (as dpkg
    # recorded it), and of the new package's template. dpkg leaves config.toml.dpkg-dist,
    # and a merge is due, only when the operator edited the file and the template changed.
    config_md5: str = ""
    shipped_md5: str = ""
    template_md5: str = ""
    merge_preview: str = ""
    merge_error: str = ""
    ufw: str | None = None  # `ufw status`, when ufw is installed
    firewalld: bool = False
    firewalld_ports: str = ""
    iptables: str = ""  # `iptables -S INPUT` and ip6tables
    ip6tables: str = ""
    nft: str = ""
    docker: bool = False
    ephemeral: tuple[int, int] = (32768, 60999)
    peers: list[Peer] = field(default_factory=list)


def nginx_error(stderr: str) -> str:
    """The line of `nginx -t` output that says what is wrong."""
    for line in stderr.splitlines():
        m = re.search(r"\[(?:emerg|alert|crit|error)\] \d+#\d+: (.*)", line)
        if m:
            return m.group(1)
    return (stderr.strip().splitlines() or ["unknown error"])[-1]


def parse_os_release(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip('"')
    return out


def parse_defaults(text: str) -> dict[str, str]:
    """KEY=VALUE lines of /etc/default/texas2step (commented ones are defaults)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def toml_settings(text: str | None) -> dict:
    if not text:
        return {}
    import tomllib

    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def toml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else str(value)


# ---- The plan --------------------------------------------------------------------------


@dataclass
class Options:
    deb: str = ""
    site: str = ""
    no_nginx: bool = False
    speedtest: bool = False
    id: str = ""
    name: str = ""
    location: str = ""
    peers: list[str] = field(default_factory=list)
    udp_ports: tuple[int, int] | None = None
    no_firewall: bool = False
    allow_downgrade: bool = False


@dataclass
class NginxEdit:
    file: str  # the real file, not a sites-enabled symlink
    sha256: str
    after: int
    lines: list[str]


@dataclass
class Plan:
    findings: list[Finding] = field(default_factory=list)
    mode: str = "fresh"  # fresh, reinstall, upgrade or downgrade
    version: str = ""  # the package's version
    app_version: str = ""  # without the Debian revision, as /api/v1/health reports it
    settings: list[str] = field(default_factory=list)  # KEY=VALUE for `config set`
    peers: list[dict[str, str]] = field(default_factory=list)  # for `config add-location`
    nginx: NginxEdit | None = None
    public: str = ""  # https://host/texas2step/ once published
    resolve: str = ""  # host:port for curl --resolve
    firewall: list[list[str]] = field(default_factory=list)  # commands
    listen: tuple[str, str] = ("127.0.0.1", "8080")
    udp: tuple[int, int] = DEFAULT_PORTS  # the probe's UDP range once installed
    merge: bool = False  # the upgrade brings the settings file up to date

    def add(self, level: str, text: str) -> None:
        self.findings.append(Finding(level, text))

    @property
    def blocked(self) -> bool:
        return any(f.level == FIX for f in self.findings)


def plan(host: Host, opts: Options) -> Plan:
    p = Plan()
    _check_system(host, opts, p)
    _check_package(host, opts, p)
    if opts.no_nginx:
        p.add(
            NOTE, "nginx left alone (--no-nginx): publish texas2step yourself, see docs/install.md"
        )
    else:
        _check_nginx(host, opts, p)
    _check_settings(host, opts, p)
    _check_firewall(host, opts, p)
    return p


def _check_system(host: Host, opts: Options, p: Plan) -> None:
    osr = host.os_release
    name = osr.get("PRETTY_NAME", "an unknown system")
    if (osr.get("ID"), osr.get("VERSION_ID")) == WANT_OS:
        p.add(OK, name)
    else:
        p.add(FIX, f"texas2step is packaged for Ubuntu 26.04 LTS; this is {name}")
    if host.systemd:
        p.add(OK, "systemd is running")
    else:
        p.add(FIX, "systemd isn't running; texas2step runs as a systemd service")


def _check_package(host: Host, opts: Options, p: Plan) -> None:
    if not host.deb_exists:
        p.add(FIX, f"package not found: {host.deb_path}; copy it next to this script or give --deb")
        return
    if DEB_SHA256.startswith("@"):
        p.add(
            NOTE, f"{host.deb_path}: this copy of texas2step-setup carries no checksum to compare"
        )
    elif host.deb_sha256 != DEB_SHA256:
        p.add(FIX, f"{host.deb_path} isn't the package this script was built with (sha256 differs)")
        return
    else:
        p.add(OK, f"{Path(host.deb_path).name}: checksum matches")
    p.version = host.deb.get("Version", "")
    p.app_version = p.version.rsplit("-", 1)[0]
    arch = host.deb.get("Architecture", "")
    if arch not in {host.arch, "all"}:
        p.add(FIX, f"the package is for {arch}; this host is {host.arch}")

    for dep in host.deps:
        need = f"{dep.name} {dep.need}".strip()
        if dep.ok:
            continue
        if dep.candidate_ok:
            have = f" (installed: {dep.installed})" if dep.installed else ""
            p.add(CHANGE, f"apt installs {need}{have}")
        else:
            p.add(FIX, f"needs {need}, which apt can't find; run sudo apt-get update and check")
    if host.deps and all(d.ok for d in host.deps):
        p.add(OK, "dependencies installed: " + ", ".join(d.name for d in host.deps))

    need_kib = int(host.deb.get("Installed-Size", "0") or 0) * 2 + 50_000
    if host.free_kib < need_kib:
        p.add(
            FIX,
            f"not enough disk space under /opt: {host.free_kib // 1024} MB free, "
            f"about {need_kib // 1024} MB needed",
        )

    installed, cmp = host.installed, host.version_cmp
    if not installed:
        p.mode = "fresh"
        p.add(CHANGE, f"install texas2step {p.version}")
    elif cmp == "eq":
        p.mode = "reinstall"
        p.add(CHANGE, f"reinstall texas2step {p.version} (already installed); settings are kept")
    elif cmp == "lt":
        p.mode = "upgrade"
        p.add(CHANGE, f"upgrade texas2step {installed} -> {p.version}; settings are kept")
    else:
        p.mode = "downgrade"
        if opts.allow_downgrade:
            p.add(CHANGE, f"downgrade texas2step {installed} -> {p.version} (--allow-downgrade)")
        else:
            p.add(
                FIX,
                f"texas2step {installed} is newer than this package ({p.version}); "
                "use the newer package, or --allow-downgrade",
            )
    if installed:
        state = "running" if host.service_active else "not running"
        p.add(OK if host.service_active else NOTE, f"texas2step {installed} is {state}")

    p.listen = (
        host.defaults.get("LISTEN_HOST", "127.0.0.1"),
        host.defaults.get("LISTEN_PORT", "8080"),
    )
    if host.port_users:
        p.add(
            FIX,
            f"port {p.listen[1]} is in use by {', '.join(host.port_users)}; texas2step "
            f"listens there (LISTEN_PORT in {DEFAULTS})",
        )


def _check_nginx(host: Host, opts: Options, p: Plan) -> None:
    if not host.nginx_installed:
        p.add(
            FIX,
            "nginx isn't installed. Install it and set up your HTTPS site first "
            "(sudo apt install nginx; certificates e.g. with certbot), or use --no-nginx",
        )
        return
    if host.nginx_error:
        p.add(FIX, f"nginx -T fails, so nginx's configuration can't be checked: {host.nginx_error}")
        return
    if not host.nginx_active:
        p.add(FIX, "nginx is installed but not running: sudo systemctl enable --now nginx")
        return
    p.add(OK, "nginx is running")
    files = split_dump(host.nginx_dump)
    found = servers(files)

    # Already published: leave nginx alone, unless --site names another site for it.
    published = [(s, path) for s in found for path in s.published]
    if published and not opts.site:
        s, path = published[0]
        p.add(OK, f"texas2step is already published at {path} by {s.label()}; nginx is left as is")
        _public(p, s, path)
        return
    if not loads_upstream(files):
        p.add(
            FIX,
            f"nginx doesn't load {UPSTREAM_CONF} (no include of conf.d/*.conf in http {{ }}), "
            "which the package's snippet needs",
        )

    chosen = _choose_site(found, opts, p)
    if not chosen:
        return
    if chosen.published:
        path = chosen.published[0]
        p.add(OK, f"texas2step is already published at {path} by {chosen.label()}")
        _public(p, chosen, path)
        return
    taken = [loc for loc in chosen.locations if loc.rstrip("/") == "/texas2step"]
    if taken:
        p.add(
            FIX,
            f"{chosen.label()} already has a location {taken[0]}; texas2step needs /texas2step/",
        )
        return
    lines = [f"{chosen.indent}include {SNIPPET};"]
    if opts.speedtest:
        if any(loc.rstrip("/") == "/speedtest" for loc in chosen.locations):
            p.add(NOTE, "--speedtest skipped: that site already uses /speedtest")
        else:
            lines.append(f"{chosen.indent}include {SPEEDTEST_SNIPPET};")
    real = os.path.realpath(chosen.file)
    disk = host.site_texts.get(real)
    if disk is None or disk.rstrip("\n") != files[chosen.file].rstrip("\n"):
        p.add(FIX, f"{real} changed while it was being checked; run texas2step-setup again")
        return
    sha = hashlib.sha256(disk.encode()).hexdigest()
    p.nginx = NginxEdit(real, sha, chosen.insert_after, lines)
    shown = ", ".join(x.strip() for x in lines)
    p.add(
        CHANGE,
        f"add {shown} to {chosen.label()} (after line {chosen.insert_after}), "
        "test with nginx -t and reload; the file is backed up first",
    )
    if not chosen.https:
        p.add(
            NOTE,
            f"{chosen.label()} is plain HTTP; fine on a closed network, but use HTTPS elsewhere",
        )
    _public(p, chosen, "/texas2step/")


def _choose_site(found: list[Server], opts: Options, p: Plan) -> Server | None:
    https = [s for s in found if s.https]
    if opts.site:
        path, _, line = opts.site.partition(":")
        real = os.path.realpath(path)
        mine = [s for s in found if os.path.realpath(s.file) == real]
        if line:
            mine = [s for s in mine if str(s.line) == line]
        if not mine:
            p.add(FIX, f"--site {opts.site}: nginx loads no server block from there")
            return None
        if len(mine) > 1:
            mine = [s for s in mine if s.https] or mine
        if len(mine) > 1:
            listed = "; ".join(s.label() for s in mine)
            p.add(
                FIX,
                f"--site {opts.site} has several server blocks ({listed}); "
                "pick one with --site FILE:LINE",
            )
            return None
        return mine[0]
    if len(https) == 1:
        p.add(OK, f"HTTPS site: {https[0].label()}")
        return https[0]
    if not https:
        p.add(
            FIX,
            "nginx serves no HTTPS site. Set one up (e.g. with certbot), or name a site "
            "with --site FILE",
        )
        return None
    listed = "\n      ".join(s.label() for s in https)
    p.add(
        FIX,
        f"nginx serves several HTTPS sites; choose one with --site FILE (or FILE:LINE):\n"
        f"      {listed}",
    )
    return None


def _public(p: Plan, s: Server, path: str) -> None:
    host = next(
        (n for n in s.names if n not in {"_", "localhost", ""} and "*" not in n), "localhost"
    )
    scheme = "https" if s.https else "http"
    port = "" if s.port == (443 if s.https else 80) else f":{s.port}"
    p.public = f"{scheme}://{host}{port}{path.rstrip('/')}/"
    p.resolve = f"{host}:{s.port}:{s.addr}"


def _check_settings(host: Host, opts: Options, p: Plan) -> None:
    current = toml_settings(host.config)
    if host.config is None:
        p.add(OK, f"{CONFIG} will be created from the package's template")
    elif p.mode in {"upgrade", "reinstall"}:  # a rebuilt package can carry a new template too
        edited = host.config_md5 != host.shipped_md5
        changed = host.template_md5 != host.shipped_md5
        if not host.shipped_md5:  # dpkg's record is missing: compare with the new template
            edited = changed = host.config_md5 != host.template_md5
        if not changed:
            p.add(OK, f"{CONFIG} stays as it is: the package's template is the same")
        elif not edited:
            p.add(OK, f"{CONFIG} was never edited, so the package's template replaces it")
        elif host.merge_error:
            p.merge = True
            p.add(NOTE, f"couldn't preview the settings merge: {host.merge_error}")
        elif "is up to date" not in host.merge_preview:
            p.merge = True
            p.add(
                CHANGE,
                f"bring {CONFIG} up to date with the package's template "
                "(texas2step config merge; your values kept, a dated backup made; preview below)",
            )

    defaults = {
        "server_id": "local",
        "server_name": "texas2step",
        "server_location": "",
        "rtc_port_min": 0,
        "rtc_port_max": 0,
    }
    wanted: dict[str, object] = {}
    for key, value in (
        ("server_id", opts.id),
        ("server_name", opts.name),
        ("server_location", opts.location),
    ):
        if value:
            wanted[key] = value
    pinned = (current.get("rtc_port_min", 0), current.get("rtc_port_max", 0))
    lo, hi = p.udp = opts.udp_ports or (pinned if pinned != (0, 0) else DEFAULT_PORTS)
    wanted.update(rtc_port_min=lo, rtc_port_max=hi)
    for key, value in wanted.items():
        old = current.get(key, defaults[key])
        if old != value:
            p.settings.append(f"{key}={toml_value(value)}")
            p.add(CHANGE, f"set {key}: {toml_value(old)} -> {toml_value(value)}")
    if not opts.id and current.get("server_id", "local") == "local":
        p.add(
            NOTE,
            'server_id stays "local"; give each server its own --id (and --name, '
            "--location) so results and the location picker tell them apart",
        )

    if host.ephemeral[0] <= hi and lo <= host.ephemeral[1]:
        p.add(
            NOTE,
            f"UDP {lo}-{hi} overlaps the kernel's ephemeral ports "
            f"({host.ephemeral[0]}-{host.ephemeral[1]}); it works, but outbound connections "
            "can take ports from it. A range such as 61000-61999 avoids that",
        )
    workers = int(host.defaults.get("WORKERS", "2") or 2)
    sessions = int(current.get("max_rtc_sessions", 32))
    if hi - lo + 1 < workers * sessions:
        p.add(
            NOTE,
            f"UDP {lo}-{hi} has {hi - lo + 1} ports; {workers} workers x {sessions} "
            f"sessions can use up to {workers * sessions}",
        )

    me = str(wanted.get("server_id", current.get("server_id", "local")))
    listed = current.get("locations", [])
    for peer in host.peers:
        url = peer.url.rstrip("/")
        info = (peer.info or {}).get("server") or {}
        if not peer.info:
            p.add(
                NOTE,
                f"peer {url} skipped: can't reach it ({peer.error}). Once it is up, "
                f"add it: sudo texas2step config add-location {url} --write",
            )
            continue
        if not info.get("id") or not info.get("name"):
            p.add(NOTE, f"peer {url} skipped: it answered, but not as a texas2step server")
            continue
        if info.get("id") == me:
            p.add(NOTE, f"peer {url} skipped: it reports server_id {me!r}, the same as this server")
            continue
        if any(
            loc.get("id") == info.get("id") or loc.get("url", "").rstrip("/") == url
            for loc in listed
        ):
            p.add(OK, f"peer {info.get('name')} ({url}) is already listed")
            continue
        entry = {
            "url": url,
            "id": info.get("id", ""),
            "name": info.get("name", ""),
            "location": info.get("location", ""),
        }
        p.peers.append(entry)
        p.add(CHANGE, f"list {entry['name']} ({entry['id']}, {url}) in the location picker")
        if url.startswith("http://") and p.public.startswith("https://"):
            p.add(
                NOTE,
                f"peer {url} is plain HTTP; browsers on the HTTPS page will block it. "
                "Use its https:// address",
            )


def _check_firewall(host: Host, opts: Options, p: Plan) -> None:
    lo, hi = p.udp
    rng = f"UDP {lo}-{hi}"
    cloud = (
        "Inbound UDP must also be allowed by anything in front of this host (cloud "
        "security groups, network firewalls), which this check can't see"
    )

    if host.ufw is not None and "Status: active" in host.ufw:
        if ufw_allows(host.ufw, lo, hi):
            p.add(OK, f"ufw allows {rng}")
        elif opts.no_firewall:
            p.add(
                NOTE,
                f"ufw doesn't allow {rng} (left alone, --no-firewall): "
                f"sudo ufw allow {lo}:{hi}/udp",
            )
        else:
            p.firewall.append(["ufw", "allow", f"{lo}:{hi}/udp", "comment", "texas2step probes"])
            p.add(CHANGE, f"allow {rng} in ufw")
    elif host.firewalld:
        if firewalld_allows(host.firewalld_ports, lo, hi):
            p.add(OK, f"firewalld allows {rng}")
        elif opts.no_firewall:
            p.add(NOTE, f"firewalld doesn't allow {rng} (left alone, --no-firewall)")
        else:
            p.firewall.append(["firewall-cmd", "--permanent", f"--add-port={lo}-{hi}/udp"])
            p.firewall.append(["firewall-cmd", "--reload"])
            p.add(CHANGE, f"allow {rng} in firewalld")
    else:
        v4 = iptables_state(host.iptables, lo, hi)
        v6 = iptables_state(host.ip6tables, lo, hi)
        nft = nft_state(host.nft, lo, hi)
        if v4[0] or v6[0]:
            missing = [cmd for (f, a), cmd in ((v4, "iptables "), (v6, "ip6tables")) if f and not a]
            if not missing:
                p.add(OK, f"iptables allows {rng}")
            else:
                cmds = "\n      ".join(
                    f"sudo {c} -I INPUT -p udp --dport {lo}:{hi} -j ACCEPT" for c in missing
                )
                keep = (
                    "and add the same rule to /etc/iptables/rules.v4 / rules.v6 so it "
                    "survives a reboot"
                )
                if host.docker:
                    keep += (
                        "; don't use netfilter-persistent save here, it would also save "
                        "Docker's own rules"
                    )
                p.add(
                    NOTE,
                    f"iptables drops {rng}; this script doesn't change iptables. "
                    f"Run:\n      {cmds}\n    {keep}",
                )
        elif nft[0]:
            if nft[1]:
                p.add(OK, f"nftables allows {rng}")
            else:
                p.add(
                    NOTE,
                    f"nftables drops {rng}; this script doesn't change hand-written "
                    f"rulesets. Add to your input chain in /etc/nftables.conf:\n"
                    f"      udp dport {lo}-{hi} accept\n    then: sudo systemctl reload nftables",
                )
        else:
            p.add(OK, "no inbound firewall filtering found on this host")
    p.add(NOTE, cloud)


# ---- Writing the report and the install script ----------------------------------------


def report(host: Host, p: Plan) -> str:
    when = time.strftime("%Y-%m-%d %H:%M %Z")
    out = [
        f"texas2step setup for {host.hostname}, {when}",
        f"Package: {host.deb_path} ({p.version or 'unknown version'})",
        f"Plan: {VERBS[p.mode]} texas2step {p.version}",
        "",
    ]
    for level in (OK, CHANGE, FIX, NOTE):
        items = [f for f in p.findings if f.level == level]
        if items:
            out.append(SECTIONS[level])
            out += [f"  - {f.text}" for f in items]
            out.append("")
    if p.merge and host.merge_preview:
        out.append("Settings merge preview (texas2step config merge, from the new version):")
        out += [f"    {line}" for line in host.merge_preview.rstrip().splitlines()]
        out.append("")
    if p.blocked:
        out.append(
            "Fix the items under 'You must fix', then run texas2step-setup again. "
            "No install script was written."
        )
    else:
        out.append(f"Next: read {INSTALL}, then run it:  sudo ./{INSTALL}")
    return "\n".join(out) + "\n"


def install_script(host: Host, p: Plan, opts: Options) -> str:
    q = shlex.quote
    deb = os.path.realpath(host.deb_path)
    plan_lines = [f.text for f in p.findings if f.level == CHANGE]
    header = "\n".join(f"#   - {line}" for line in plan_lines) or "#   - nothing to change"
    out = [
        "#!/usr/bin/env bash",
        f"{MARK} for {host.hostname} on {time.strftime('%Y-%m-%d %H:%M %Z')}.",
        "# Read it, then run:  sudo ./" + INSTALL,
        "#",
        "# Plan:",
        header,
        "#",
        "# Safe to run again: each step checks what is already done.",
        "set -euo pipefail",
        "",
        'if [ "$(id -u)" -ne 0 ]; then echo "Run it with sudo: sudo $0" >&2; exit 1; fi',
        'here=$(cd "$(dirname "$0")" && pwd)',
        'exec > >(tee -a "$here/texas2step-install.log") 2>&1',
        "step() { printf '\\n== %s\\n' \"$*\"; }",
        'echo "texas2step install, $(date -R)"',
        "",
        f"deb={q(deb)}",
        f"sha256={q(host.deb_sha256)}",
        f"version={q(p.version)}",
        f"app_version={q(p.app_version)}",
        f"allow_downgrade={'yes' if opts.allow_downgrade else 'no'}",
        "",
        'step "Checking the package"',
        'echo "$sha256  $deb" | sha256sum --check --strict',
        "",
        "installed=$(dpkg-query -W --showformat='${db:Status-Abbrev} ${Version}' texas2step"
        " 2>/dev/null | awk '$1 == \"ii\" {print $2}' || true)",
        'if [ -n "$installed" ] && dpkg --compare-versions "$installed" gt "$version" '
        '&& [ "$allow_downgrade" != yes ]; then',
        '    echo "texas2step $installed is newer than $version; not downgrading." >&2',
        "    exit 1",
        "fi",
        "flags=()",
        '[ "$installed" = "$version" ] && flags+=(--reinstall)',
        '[ "$allow_downgrade" = yes ] && flags+=(--allow-downgrades)',
        'step "Installing texas2step $version${installed:+ (was $installed)}"',
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Options::=--force-confold "
        '"${flags[@]}" "$deb"',
        "",
        "# dpkg kept the settings file and left the new template beside it: merge them.",
        f"if [ -e {CONFIG}.dpkg-dist ]; then",
        '    step "Bringing the settings up to date"',
        "    texas2step config merge --write",
        f"    rm -f {CONFIG}.dpkg-dist",
        f'    echo "removed {CONFIG}.dpkg-dist: the new template is merged in"',
        "fi",
    ]
    if p.settings or p.peers:
        out += ["", 'step "Settings"']
        if p.settings:
            out.append(shlex.join(["texas2step", "config", "set", *p.settings, "--write"]))
        for peer in p.peers:
            args = [
                "texas2step",
                "config",
                "add-location",
                peer["url"],
                "--id",
                peer["id"],
                "--name",
                peer["name"],
            ]
            if peer.get("location"):
                args += ["--location", peer["location"]]
            out.append(shlex.join([*args, "--write"]))
    out += ["", 'step "Restarting texas2step"', "systemctl restart texas2step"]

    if p.nginx:
        e = p.nginx
        block = "\\n".join(e.lines)
        out += [
            "",
            f'step "Publishing it through nginx ({e.file})"',
            f"site={q(e.file)}",
            f"if grep -qE '^[[:space:]]*include[[:space:]]+{re.escape(SNIPPET)};' \"$site\"; then",
            '    echo "already included in $site"',
            "else",
            f'    if ! echo {q(e.sha256)}"  $site" | sha256sum --check --status; then',
            '        echo "$site changed since texas2step-setup checked it;" \\',
            '            "run texas2step-setup again." >&2',
            "        exit 1",
            "    fi",
            "    mkdir -p /var/backups/texas2step",
            '    backup="/var/backups/texas2step/$(basename "$site").$(date +%Y%m%d-%H%M%S)"',
            '    cp -a "$site" "$backup"',
            f"    awk -v n={e.after} -v add={q(block)} "
            '\'{print} NR == n {print add}\' "$backup" > "$site"',
            "    if ! nginx -t; then",
            '        cat "$backup" > "$site"',
            '        echo "nginx -t failed; $site is back as it was (copy: $backup)." >&2',
            "        exit 1",
            "    fi",
            "    systemctl reload nginx",
            '    echo "added the include; the previous file is $backup"',
            "fi",
        ]
    if p.firewall:
        out += ["", 'step "Firewall"'] + [shlex.join(cmd) for cmd in p.firewall]

    health = f"http://{p.listen[0]}:{p.listen[1]}/api/v1/health"
    out += [
        "",
        'step "Checking"',
        "for _ in $(seq 1 30); do",
        f"    if curl -fsS {q(health)} 2>/dev/null \\",
        """        | grep -q '"version":"'"$app_version"'"'; then""",
        "        ok=yes; break",
        "    fi",
        "    sleep 1",
        "done",
        'if [ "${ok:-}" != yes ]; then',
        f'    echo "texas2step $app_version isn\'t answering on {health};" \\',
        '        "see: journalctl -u texas2step" >&2',
        "    exit 1",
        "fi",
        f'echo "texas2step $app_version answers on {health}"',
    ]
    if p.public:
        url = p.public + "api/v1/health"
        failed = f"but not through nginx at {p.public}; check the site and nginx -T"
        out += [
            f"if curl -fsSk --resolve {q(p.resolve)} {q(url)} >/dev/null; then",
            f"    echo {q('and through nginx at ' + p.public)}",
            "else",
            f"    echo {q(failed)} >&2",
            "    exit 1",
            "fi",
            "",
            f"echo; echo {q('Done. Open ' + p.public)}",
        ]
    else:
        out += [
            "",
            "echo; echo 'Done. texas2step is installed; publish it through your web server.'",
        ]
    return "\n".join(out) + "\n"


# ---- Gathering facts (the only part that touches the system, read-only) ---------------


def run(*cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return r.returncode, r.stdout, r.stderr


def read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def dpkg_version(package: str) -> str | None:
    rc, out, _ = run("dpkg-query", "-W", "--showformat=${db:Status-Abbrev} ${Version}", package)
    words = out.split()
    return words[1] if rc == 0 and len(words) == 2 and words[0] == "ii" else None


def version_satisfies(version: str, need: str) -> bool:
    if not need:
        return True
    op, _, want = need.partition(" ")
    ops = {">=": "ge", "<=": "le", ">>": "gt", "<<": "lt", "=": "eq"}
    return run("dpkg", "--compare-versions", version, ops.get(op, "ge"), want)[0] == 0


def parse_depends(text: str) -> list[tuple[str, str]]:
    deps = []
    for part in text.split(","):
        first = part.split("|")[0].strip()
        m = re.match(r"^([a-z0-9.+-]+)(?::\w+)?\s*(?:\(\s*([<>=]+)\s*([^)\s]+)\s*\))?$", first)
        if m:
            deps.append((m.group(1), f"{m.group(2)} {m.group(3)}" if m.group(2) else ""))
    return deps


def gather(opts: Options, deb_path: str) -> Host:
    h = Host(hostname=socket.getfqdn(), deb_path=deb_path)
    h.os_release = parse_os_release(read("/etc/os-release") or "")
    h.arch = run("dpkg", "--print-architecture")[1].strip()
    h.systemd = os.path.isdir("/run/systemd/system")

    h.deb_exists = os.path.isfile(deb_path)
    if h.deb_exists:
        digest = hashlib.sha256()
        with open(deb_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        h.deb_sha256 = digest.hexdigest()
        for key in ("Package", "Version", "Architecture", "Depends", "Installed-Size"):
            h.deb[key] = run("dpkg-deb", "-f", deb_path, key)[1].strip()
        merged: dict[str, list[str]] = {}
        for name, need in parse_depends(h.deb.get("Depends", "")):
            merged.setdefault(name, []).append(need)
        for name, needs in merged.items():
            installed = dpkg_version(name)
            ok = installed is not None and all(version_satisfies(installed, n) for n in needs)
            candidate_ok = ok
            if not ok:
                out = run("apt-cache", "policy", name)[1]
                m = re.search(r"Candidate:\s*(\S+)", out)
                cand = m.group(1) if m and m.group(1) != "(none)" else None
                candidate_ok = cand is not None and all(version_satisfies(cand, n) for n in needs)
            need = ", ".join(n for n in needs if n)
            h.deps.append(Dep(name, need, installed, ok, candidate_ok))

    probe = "/opt" if os.path.isdir("/opt") else "/"
    st = os.statvfs(probe)
    h.free_kib = st.f_bavail * st.f_frsize // 1024

    h.nginx_installed = shutil.which("nginx") is not None or os.path.exists("/usr/sbin/nginx")
    if h.nginx_installed:
        h.nginx_active = run("systemctl", "is-active", "--quiet", "nginx")[0] == 0
        rc, out, err = run("nginx", "-T")
        if rc == 0:
            h.nginx_dump = out
            for f in split_dump(out):
                real = os.path.realpath(f)
                h.site_texts[real] = read(real) or ""
        else:
            h.nginx_error = nginx_error(err)

    h.defaults = {"LISTEN_HOST": "127.0.0.1", "LISTEN_PORT": "8080", "WORKERS": "2"}
    h.defaults.update(parse_defaults(read(DEFAULTS) or ""))
    port = h.defaults["LISTEN_PORT"]
    out = run("ss", "-Hltnp", f"sport = :{port}")[1]
    for pid in sorted(set(re.findall(r"pid=(\d+)", out))):
        cmdline = (read(f"/proc/{pid}/cmdline") or "").replace("\0", " ")
        cgroup = read(f"/proc/{pid}/cgroup") or ""
        if "texas2step" not in cmdline + cgroup:
            name = (read(f"/proc/{pid}/comm") or "?").strip()
            h.port_users.append(f"{name} (pid {pid})")
    if out.strip() and not re.search(r"pid=\d+", out):
        h.port_users.append("an unknown process")

    h.installed = dpkg_version("texas2step")
    if h.installed and h.deb.get("Version"):
        for op in ("lt", "eq", "gt"):
            if run("dpkg", "--compare-versions", h.installed, op, h.deb["Version"])[0] == 0:
                h.version_cmp = op
                break
    h.service_active = run("systemctl", "is-active", "--quiet", "texas2step")[0] == 0
    h.config = read(CONFIG)
    if h.installed and h.version_cmp in {"lt", "eq"} and h.config is not None and h.deb_exists:
        h.config_md5 = hashlib.md5(Path(CONFIG).read_bytes()).hexdigest()
        files = run("dpkg-query", "-W", "--showformat=${Conffiles}", "texas2step")[1]
        m = re.search(rf"^\s*{re.escape(CONFIG)} ([0-9a-f]{{32}})", files, re.M)
        h.shipped_md5 = m.group(1) if m else ""
        h.template_md5 = packaged_md5(deb_path, CONFIG)
        if h.config_md5 != h.shipped_md5 and h.template_md5 != h.shipped_md5:
            h.merge_preview, h.merge_error = preview_merge(deb_path)

    if shutil.which("ufw"):
        h.ufw = run("ufw", "status")[1]
    h.firewalld = run("firewall-cmd", "--state")[1].strip() == "running"
    if h.firewalld:
        h.firewalld_ports = run("firewall-cmd", "--list-ports")[1]
    h.iptables = run("iptables", "-S", "INPUT")[1]
    h.ip6tables = run("ip6tables", "-S", "INPUT")[1]
    h.nft = run("nft", "list", "ruleset")[1]
    h.docker = shutil.which("docker") is not None or os.path.exists("/var/run/docker.sock")
    lo_hi = (read("/proc/sys/net/ipv4/ip_local_port_range") or "").split()
    if len(lo_hi) == 2:
        h.ephemeral = (int(lo_hi[0]), int(lo_hi[1]))

    for url in opts.peers:
        h.peers.append(fetch_peer(url))
    return h


def packaged_md5(deb_path: str, path: str) -> str:
    """md5 of one file inside the package, read straight from its data archive."""
    import tarfile

    proc = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", deb_path], stdout=subprocess.PIPE)
    digest = ""
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.name.lstrip(".") == path and member.isfile():
                    f = tar.extractfile(member)
                    digest = hashlib.md5(f.read()).hexdigest() if f else ""
                    break
    except (OSError, tarfile.TarError):
        pass
    finally:
        proc.kill()
        proc.wait()
    return digest


def preview_merge(deb_path: str) -> tuple[str, str]:
    """What the new version's `config merge` would change, run from the unpacked package."""
    with tempfile.TemporaryDirectory(prefix="texas2step-setup-") as tmp:
        rc, _, err = run("dpkg-deb", "-x", deb_path, tmp, timeout=300)
        if rc != 0:
            return "", f"dpkg-deb -x failed: {err.strip()}"
        python = os.path.join(tmp, "opt/texas2step/venv/bin/python")
        env_free = {k: v for k, v in os.environ.items() if not k.startswith("TEXAS2STEP_")}
        try:
            r = subprocess.run(
                [python, "-m", "texas2step", "config", "merge", "--config", CONFIG],
                capture_output=True,
                text=True,
                timeout=120,
                env=env_free,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "", str(exc)
        if r.returncode != 0:
            return "", (r.stderr.strip() or r.stdout.strip()).splitlines()[-1]
        keep = [x for x in r.stdout.splitlines() if not x.startswith("Nothing written")]
        return "\n".join(keep), ""


def fetch_peer(url: str) -> Peer:
    peer = Peer(url)
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/v1/info", headers={"User-Agent": "texas2step-setup"}
        )
        with urllib.request.urlopen(req, timeout=8, context=ssl.create_default_context()) as r:
            peer.info = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        peer.error = str(reason)
    return peer


def _ports(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)[-:](\d+)", value)
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)) <= 65535:
        raise argparse.ArgumentTypeError("expected MIN-MAX, e.g. 61000-61999")
    return int(m.group(1)), int(m.group(2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="texas2step-setup",
        description="Check this host for texas2step and write an install script for it. "
        "Changes nothing on the system.",
    )
    ap.add_argument("--deb", help=f"the package (default: {DEB_NAME} next to this script)")
    ap.add_argument("--site", help="nginx site file to publish texas2step on (FILE or FILE:LINE)")
    ap.add_argument("--no-nginx", action="store_true", help="don't publish it through nginx")
    ap.add_argument("--speedtest", action="store_true", help="also redirect /speedtest to it")
    ap.add_argument("--id", default="", help="this server's short id, e.g. houston")
    ap.add_argument("--name", default="", help="this server's name, e.g. 'houston.example.net'")
    ap.add_argument("--location", default="", help="where it is, e.g. 'Houston, TX'")
    ap.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="URL",
        help="another texas2step server to list in the picker (repeatable)",
    )
    ap.add_argument(
        "--udp-ports",
        type=_ports,
        metavar="MIN-MAX",
        help="UDP range for the probe (default: the configured one, else 61000-61999)",
    )
    ap.add_argument("--no-firewall", action="store_true", help="don't change the firewall")
    ap.add_argument("--allow-downgrade", action="store_true", help="install an older version")
    ap.add_argument("--out", default=".", help="where to write the report and script")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print(
            "texas2step-setup reads nginx, firewall and package state, which needs root.\n"
            "It changes nothing. Run it with sudo: sudo ./texas2step-setup",
            file=sys.stderr,
        )
        return 2
    here = os.path.dirname(os.path.realpath(__file__))
    default_deb = os.path.join(here, DEB_NAME) if not DEB_NAME.startswith("@") else ""
    deb = args.deb or default_deb
    if not deb:
        ap.error("--deb is required for an unbuilt copy of this script")
    opts = Options(
        deb=deb,
        site=args.site or "",
        no_nginx=args.no_nginx,
        speedtest=args.speedtest,
        id=args.id,
        name=args.name,
        location=args.location,
        peers=args.peer,
        udp_ports=args.udp_ports,
        no_firewall=args.no_firewall,
        allow_downgrade=args.allow_downgrade,
    )
    host = gather(opts, os.path.abspath(deb))
    p = plan(host, opts)
    text = report(host, p)
    print(text, end="")

    out = Path(args.out)
    owner = (int(os.environ.get("SUDO_UID", "0")), int(os.environ.get("SUDO_GID", "0")))
    if not out.is_dir():
        out.mkdir(parents=True)
        os.chown(out, *owner)  # the operator's, like the files in it
    (out / REPORT).write_text(text)
    os.chown(out / REPORT, *owner)
    script = out / INSTALL
    if p.blocked:
        if script.exists() and MARK in script.read_text(errors="replace"):
            script.unlink()  # an older plan must not be run by mistake
        return 1
    script.write_text(install_script(host, p, opts))
    script.chmod(0o755)
    os.chown(script, *owner)
    print(f"Wrote {out / REPORT} and {script}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
