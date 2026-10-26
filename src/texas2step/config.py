"""Runtime settings.

Settings are resolved in this order (later wins):

1. Defaults declared on :class:`Settings`.
2. A TOML file named by ``TEXAS2STEP_CONFIG`` (or passed to ``--config``).
3. Environment variables ``TEXAS2STEP_<FIELD>`` (e.g. ``TEXAS2STEP_SERVER_NAME``).
   Lists are comma separated; booleans accept 1/0, true/false, yes/no, on/off.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import tomllib
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .access import INTERVALS

ENV_PREFIX = "TEXAS2STEP_"
PAGES = ("test", "monitor", "diag", "history", "api")  # the web UI's tabs, in order
THEMES = ("system", "light", "dark", "america", "energy", "federal", "military", "texas")
# Branding URLs end up in href attributes, so only these schemes are accepted.
LINK_SCHEMES = ("https://", "http://", "mailto:", "tel:")
# A link inside brand_footer's text, written as in Markdown: [label](url).
FOOTER_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
CREDITS = ("link", "text", "none")  # brand_credit
TRAFFIC_COUNTS = ("sent", "received", "both")
MONITOR_PROFILES = ("path", "voice", "video")  # monitor_profile


@dataclass(frozen=True)
class Location:
    """A texas2step server the browser can test against."""

    id: str
    name: str
    url: str = ""  # base URL; empty means "this server"
    location: str = ""  # free text, e.g. "Dallas, TX"
    url_v4: str = ""  # optional IPv4-only hostname for forcing the address family
    url_v6: str = ""  # optional IPv6-only hostname
    # Optional single-family names that each serve one HTTP version, for Diag's HTTP/1.1
    # and HTTP/3 rows; url_v4 and url_v6 are its HTTP/2 rows.
    url_v4_http1: str = ""
    url_v4_http3: str = ""
    url_v6_http1: str = ""
    url_v6_http3: str = ""

    def public(self) -> dict[str, str]:
        return dataclasses.asdict(self)

    def urls(self) -> list[str]:
        """Every base URL it may be reached at, empty where it isn't set."""
        return [self.url, self.url_v4, self.url_v6, *(getattr(self, k) for k in PROTOCOL_URLS)]


PROTOCOL_URLS = ("url_v4_http1", "url_v4_http3", "url_v6_http1", "url_v6_http3")


@dataclass(frozen=True)
class Link:
    """A link in the page header and footer, such as the IT service desk."""

    label: str
    url: str


def proxy_problem(url: str) -> str:
    """Why ``url`` isn't a proxy to download through, or "": http:// or https://, a host,
    and a port if it isn't the scheme's own."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises for a port that isn't a number
    except ValueError as exc:
        return f"{url!r} isn't a URL: {exc}"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"expected http://host:port or https://host:port, not {url!r}"
    return ""


@dataclass
class Settings:
    # Identity of this server, shown in the UI and returned by /api/v1/info.
    server_id: str = "local"
    server_name: str = "texas2step"
    server_location: str = ""
    # Optional single-family hostnames for this server (enables the IPv4/IPv6 switch).
    server_url_v4: str = ""
    server_url_v6: str = ""
    # Optional single-family hostnames that each serve one HTTP version (Diag's rows).
    server_url_v4_http1: str = ""
    server_url_v4_http3: str = ""
    server_url_v6_http1: str = ""
    server_url_v6_http3: str = ""

    # Wire endpoint limits.
    max_download_bytes: int = 1 << 30
    max_upload_bytes: int = 1 << 30
    max_stream_seconds: float = 60.0

    # UDP probing over WebRTC data channels.
    webrtc_enabled: bool = True
    ice_servers: list[str] = field(default_factory=list)
    max_rtc_sessions: int = 32
    rtc_session_timeout: float = 120.0
    # Inclusive UDP port range for the probe's server-side sockets, so a firewall can
    # allow just that range. Both 0 leaves the choice of ports to the OS.
    rtc_port_min: int = 0
    rtc_port_max: int = 0

    # Result history (SQLite).
    results_enabled: bool = True
    results_path: str = "texas2step.db"
    results_max_rows: int = 10_000

    # Server-side measurement functions (/api/v1/measure/*).
    measure_enabled: bool = True
    allow_any_target: bool = False

    # The web UI's pages: any can be hidden, but not all. The API behind them stays on;
    # results_enabled and measure_enabled turn parts of it off.
    page_test: bool = True
    page_monitor: bool = True
    page_diag: bool = True
    page_history: bool = True
    page_api: bool = True

    # Defaults offered to the browser UI.
    default_streams: int = 4
    default_seconds: float = 10.0
    default_packets: int = 500
    # Whether a test downloads over one stream too, unless the visitor or the command line
    # chooses: up to half again a test's traffic, so a server whose traffic is billed may
    # want it off.
    default_one_stream: bool = True
    # Monitor's profile until a visitor picks one: path (small probes, as a ping tracker),
    # voice (a voice call's packets) or video (a video call's, with its voice). Voice and
    # video count toward a traffic limit. monitor_voice = false and monitor_video = false
    # withhold those profiles: the page doesn't offer them, and the server refuses them.
    # Path is always offered.
    monitor_profile: str = "path"
    monitor_voice: bool = True
    monitor_video: bool = True

    # HTTP plumbing.
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    trusted_proxies: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])

    # Logging: off, only warnings and failed requests are logged; on, every request and
    # the WebRTC libraries' detail as well. Read by `texas2step serve` at start.
    debug: bool = False

    # Where `texas2step update` looks for releases: a GitHub repository's API address,
    # or a mirror that answers <update_url>/releases/latest the same way, on a web
    # server or in a directory (file:///opt/texas2step/mirror).
    update_url: str = "https://api.github.com/repos/robert-patrick-texas/texas2step"
    # A proxy for its downloads, the update timer's too, such as
    # "http://proxy.example.net:3128"; empty: https_proxy from the environment, if set.
    update_proxy: str = ""

    # Internet-facing servers (docs/access.md). access_code asks visitors from outside
    # internal_networks for the service desk's code before they can test: "off", or how
    # often the code changes: "daily", "weekly", "monthly" or "fixed".
    access_code: str = "off"
    access_timezone: str = "America/New_York"  # when daily, weekly and monthly codes change
    access_words: str = ""  # the site's own list, one entry per line; empty: built-in words
    access_digits: int = 2  # the digits that end a code
    # How long a pass lasts from when it was issued, as passes are checked: a change
    # applies to passes already given (texas2step access passes).
    access_pass_hours: float = 12.0
    # The line under the code box, such as how to reach the desk; "" for none.
    access_note: str = "Contact the service desk for assistance."
    access_key_file: str = "/etc/texas2step/access.key"  # the same key on every server
    # Standing phrases that always work as codes (texas2step access phrase); none without it.
    access_phrases_file: str = "/etc/texas2step/access-phrases.toml"
    # Wrong codes count against the visitor's /24 or /48; this many in a row lock it out
    # until no code has come from it for access_lockout_minutes (texas2step access lockouts).
    access_lockout_after: int = 3
    access_lockout_minutes: float = 30.0
    access_lockouts_file: str = "/var/lib/texas2step/access.db"  # shared by the workers
    # Servers that defend together: a lockout here, texas2step access block and access
    # lockouts --clear go to every server in [[locations]]. Each takes them only from
    # access_peer_networks (the other servers' addresses), signed with a key derived from
    # the shared access key.
    access_share_lockouts: bool = False
    access_peer_networks: list[str] = field(default_factory=list)
    # A program run on each lockout, block and clear here: EVENT NETWORK UNTIL, as the
    # service's user. Such as to tell a SIEM or a firewall's API.
    access_lockout_hook: str = ""
    # The organization's own networks (addresses or CIDR blocks): no code is asked there,
    # and only they get saved results, the API explorer and the server-side measurements.
    internal_networks: list[str] = field(default_factory=list)

    # A cap on the traffic tests move, for servers whose traffic is billed (docs/traffic.md):
    # gigabytes (10^9 bytes) a day and a month, 0 for none. Days begin at midnight in
    # access_timezone. Reached, it refuses new tests until the day or month is over, with
    # traffic_message and, if traffic_url is set, "Click here to continue." linking to it;
    # running tests finish.
    # texas2step traffic shows the figures.
    traffic_day_gb: float = 0.0
    traffic_month_gb: float = 0.0
    traffic_counts: str = "both"  # what counts: "sent", "received" or "both"
    traffic_count_internal: bool = True  # false: internal_networks test without counting
    traffic_file: str = "/var/lib/texas2step/traffic.db"  # shared by the workers
    traffic_message: str = "Testing temporarily disabled."
    traffic_url: str = ""  # such as another texas2step server, or the service desk's page

    # Other servers offered in the location picker (this server is always first).
    locations: list[Location] = field(default_factory=list)

    # Branding of the web UI.
    brand_name: str = "texas2step"
    brand_organization: str = ""
    brand_url: str = ""  # the organization's website; its name links there
    brand_logo: str = ""  # an SVG, PNG, JPEG or WebP file shown in place of the star mark
    brand_notice: str = ""  # a line across the top of every page, e.g. an acceptable-use notice
    brand_links: list[Link] = field(default_factory=list)
    # The organization's own footer: its text, line by line, with [label](url) links in it,
    # and its links beside the text.
    brand_footer: str = ""
    brand_footer_links: list[Link] = field(default_factory=list)
    default_theme: str = "system"  # one of THEMES; visitors can still pick another
    # The footer's "texas2step <version>": linked to the project's site, plain text, or none.
    brand_credit: str = "link"

    def __post_init__(self) -> None:
        # However a setting was given (file, environment, `config set`), spaces around its
        # text are no part of it: a stray space inside quotes would otherwise pass here and
        # fail later, as in an address network that every request is checked against.
        for f in dataclasses.fields(self):
            setattr(self, f.name, _trimmed(getattr(self, f.name)))
        ports = (self.rtc_port_min, self.rtc_port_max)
        if ports != (0, 0) and not 1 <= ports[0] <= ports[1] <= 65535:
            raise ValueError(
                f"rtc_port_min/rtc_port_max: need 1 <= min <= max <= 65535, or both 0; got {ports}"
            )
        if not self.pages:
            raise ValueError(
                "page_test, page_monitor, page_diag, page_history, page_api: keep one shown"
            )
        if self.default_theme not in THEMES:
            raise ValueError(f"default_theme: expected one of {', '.join(THEMES)}")
        if self.monitor_profile not in MONITOR_PROFILES:
            raise ValueError(f"monitor_profile: expected one of {', '.join(MONITOR_PROFILES)}")
        for name, offered in (("voice", self.monitor_voice), ("video", self.monitor_video)):
            if self.monitor_profile == name and not offered:
                raise ValueError(
                    f"monitor_profile: {name}, but monitor_{name} = false withholds it"
                )
        if self.brand_credit not in CREDITS:
            raise ValueError(f"brand_credit: expected one of {', '.join(CREDITS)}")
        if not self.update_url.startswith(("https://", "http://", "file:///")):
            raise ValueError("update_url: expected an https:// URL, or file:/// and a directory")
        if self.update_proxy and (why := proxy_problem(self.update_proxy)):
            raise ValueError(f"update_proxy: {why}")
        if self.access_code not in INTERVALS:
            raise ValueError(f"access_code: expected one of {', '.join(INTERVALS)}")
        try:
            ZoneInfo(self.access_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"access_timezone: {self.access_timezone!r} isn't a time zone, "
                "e.g. America/New_York or UTC"
            ) from None
        if not 2 <= self.access_digits <= 4:
            raise ValueError("access_digits: expected 2, 3 or 4")
        if not 1 <= self.access_pass_hours <= 168:
            raise ValueError("access_pass_hours: expected 1 to 168 hours")
        if not 1 <= self.access_lockout_after <= 100:
            raise ValueError("access_lockout_after: expected 1 to 100 wrong codes")
        if not 1 <= self.access_lockout_minutes <= 10080:
            raise ValueError("access_lockout_minutes: expected 1 to 10080 minutes (a week)")
        for net in self.internal_networks:
            try:
                ipaddress.ip_network(net.strip(), strict=False)
            except ValueError:
                raise ValueError(
                    f"internal_networks: {net!r} isn't an address or a network such as 10.0.0.0/8"
                ) from None
        for net in self.access_peer_networks:
            try:
                ipaddress.ip_network(net.strip(), strict=False)
            except ValueError:
                raise ValueError(
                    f"access_peer_networks: {net!r} isn't an address or a network"
                ) from None
        if self.access_share_lockouts and self.access_code == "off":
            raise ValueError("access_share_lockouts: needs access codes on (access_code)")
        if self.access_share_lockouts and not self.access_peer_networks:
            raise ValueError(
                "access_share_lockouts: list the other servers' addresses in "
                "access_peer_networks; their blocks are taken only from there"
            )
        if self.access_lockout_hook and not self.access_lockout_hook.startswith("/"):
            raise ValueError("access_lockout_hook: give the program's full path")
        if self.internal_networks and not {"test", "monitor", "diag"} & set(self.pages):
            raise ValueError(
                "internal_networks: keep page_test, page_monitor or page_diag shown, "
                "for visitors from outside them"
            )
        for name in ("traffic_day_gb", "traffic_month_gb"):
            if not 0 <= getattr(self, name) <= 1_000_000:
                raise ValueError(f"{name}: expected 0 (no limit) to 1000000 gigabytes")
        if self.traffic_counts not in TRAFFIC_COUNTS:
            raise ValueError(f"traffic_counts: expected one of {', '.join(TRAFFIC_COUNTS)}")
        if not self.traffic_message.strip():
            raise ValueError("traffic_message: must not be empty")
        if not self.brand_name.strip():
            raise ValueError("brand_name: must not be empty")
        urls = [("brand_url", self.brand_url)] if self.brand_url else []
        if self.traffic_url:
            urls.append(("traffic_url", self.traffic_url))
        for name, links in (
            ("brand_links", self.brand_links),
            ("brand_footer_links", self.brand_footer_links),
        ):
            for link in links:
                if not link.label.strip():
                    raise ValueError(f"{name}: every link needs a label")
                urls.append((f"{name} ({link.label})", link.url))
        for label, url in FOOTER_LINK.findall(self.brand_footer):
            urls.append((f"brand_footer ({label})", url))
        for name, url in urls:
            if not url.lower().startswith(LINK_SCHEMES):
                raise ValueError(f"{name}: URL must start with {', '.join(LINK_SCHEMES)}")

    @property
    def traffic_limited(self) -> bool:
        return bool(self.traffic_day_gb or self.traffic_month_gb)

    @property
    def pages(self) -> list[str]:
        """The web UI's pages this site shows, in tab order."""
        return [page for page in PAGES if getattr(self, f"page_{page}")]

    @property
    def rtc_ports(self) -> tuple[int, int] | None:
        """The UDP port range for WebRTC sockets, or None for OS-chosen ports."""
        if self.rtc_port_min == self.rtc_port_max == 0:
            return None
        return self.rtc_port_min, self.rtc_port_max

    def all_locations(self) -> list[Location]:
        me = Location(
            id=self.server_id,
            name=self.server_name,
            location=self.server_location,
            url_v4=self.server_url_v4,
            url_v6=self.server_url_v6,
            **{key: getattr(self, f"server_{key}") for key in PROTOCOL_URLS},
        )
        return [me, *[loc for loc in self.locations if loc.id != self.server_id]]

    def is_allowed_target(self, url: str) -> bool:
        if self.allow_any_target:
            return True
        wanted = url.rstrip("/")
        return any(
            wanted in {loc.url.rstrip("/"), loc.url_v4.rstrip("/"), loc.url_v6.rstrip("/")}
            for loc in self.locations
            if loc.url
        )


def _trimmed(value: Any) -> Any:
    """A setting with the whitespace around each piece of its text taken off: in lists, and
    in the fields of [[locations]] and links too."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_trimmed(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: _trimmed(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return dataclasses.replace(value, **fields)
    return value


def _coerce(raw: Any, hint: Any, name: str) -> Any:
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        hint = next(a for a in typing.get_args(hint) if a is not type(None))
        origin = typing.get_origin(hint)
    if hint is bool:
        if isinstance(raw, bool):
            return raw
        value = str(raw).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"{name}: expected a boolean, got {raw!r}")
    if hint is int:
        return int(raw)
    if hint is float:
        return float(raw)
    if hint is str:
        return str(raw).strip()
    if origin is list:
        (item,) = typing.get_args(hint)
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
        if dataclasses.is_dataclass(item):
            try:
                return [item(**entry) for entry in raw]
            except TypeError as exc:
                raise ValueError(f"{name}: {exc}") from exc
        return [_coerce(entry, item, name) for entry in raw]
    return raw


def parse_toml(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Settings values from parsed TOML; unknown keys are an error, so typos don't pass."""
    hints = typing.get_type_hints(Settings)
    unknown = set(data) - set(hints)
    if unknown:
        raise ValueError(f"unknown settings in {source}: {', '.join(sorted(unknown))}")
    return {k: _coerce(v, hints[k], k) for k, v in data.items()}


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    hints = typing.get_type_hints(Settings)
    values: dict[str, Any] = {}

    path = config_path or os.environ.get(ENV_PREFIX + "CONFIG")
    if path:
        values.update(parse_toml(tomllib.loads(Path(path).read_text()), str(path)))

    for name, hint in hints.items():
        env = os.environ.get(ENV_PREFIX + name.upper())
        if env is not None and name not in {"locations", "brand_links", "brand_footer_links"}:
            values[name] = _coerce(env, hint, name)

    return Settings(**values)
