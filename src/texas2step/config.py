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
CREDITS = ("link", "text", "none")  # brand_credit


@dataclass(frozen=True)
class Location:
    """A texas2step server the browser can test against."""

    id: str
    name: str
    url: str = ""  # base URL; empty means "this server"
    location: str = ""  # free text, e.g. "Dallas, TX"
    url_v4: str = ""  # optional IPv4-only hostname for forcing the address family
    url_v6: str = ""  # optional IPv6-only hostname

    def public(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Link:
    """A link in the page header and footer, such as the IT service desk."""

    label: str
    url: str


@dataclass
class Settings:
    # Identity of this server, shown in the UI and returned by /api/v1/info.
    server_id: str = "local"
    server_name: str = "texas2step"
    server_location: str = ""
    # Optional single-family hostnames for this server (enables the IPv4/IPv6 switch).
    server_url_v4: str = ""
    server_url_v6: str = ""

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

    # Internet-facing servers (docs/access.md). access_code asks visitors from outside
    # internal_networks for the service desk's code before they can test: "off", or how
    # often the code changes: "daily", "weekly", "monthly" or "fixed".
    access_code: str = "off"
    access_timezone: str = "America/New_York"  # when daily, weekly and monthly codes change
    access_words: str = ""  # the site's own list, one entry per line; empty: built-in words
    access_digits: int = 2  # the digits that end a code
    access_pass_hours: float = 12.0  # how long an entered code lets a browser keep testing
    access_key_file: str = "/etc/texas2step/access.key"  # the same key on every server
    # Standing phrases that always work as codes (texas2step access phrase); none without it.
    access_phrases_file: str = "/etc/texas2step/access-phrases.toml"
    # Wrong codes count against the visitor's /24 or /48; this many in a row lock it out
    # until no code has come from it for access_lockout_minutes (texas2step access lockouts).
    access_lockout_after: int = 3
    access_lockout_minutes: float = 30.0
    access_lockouts_file: str = "/var/lib/texas2step/access.db"  # shared by the workers
    # The organization's own networks (addresses or CIDR blocks): no code is asked there,
    # and only they get saved results, the API explorer and the server-side measurements.
    internal_networks: list[str] = field(default_factory=list)

    # Other servers offered in the location picker (this server is always first).
    locations: list[Location] = field(default_factory=list)

    # Branding of the web UI.
    brand_name: str = "texas2step"
    brand_organization: str = ""
    brand_url: str = ""  # the organization's website; its name links there
    brand_logo: str = ""  # an SVG, PNG, JPEG or WebP file shown in place of the star mark
    brand_notice: str = ""  # a line across the top of every page, e.g. an acceptable-use notice
    brand_links: list[Link] = field(default_factory=list)
    default_theme: str = "system"  # one of THEMES; visitors can still pick another
    # The footer's "texas2step <version>": linked to the project's site, plain text, or none.
    brand_credit: str = "link"

    def __post_init__(self) -> None:
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
        if self.brand_credit not in CREDITS:
            raise ValueError(f"brand_credit: expected one of {', '.join(CREDITS)}")
        if not self.update_url.startswith(("https://", "http://", "file:///")):
            raise ValueError("update_url: expected an https:// URL, or file:/// and a directory")
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
        if self.internal_networks and not {"test", "monitor", "diag"} & set(self.pages):
            raise ValueError(
                "internal_networks: keep page_test, page_monitor or page_diag shown, "
                "for visitors from outside them"
            )
        if not self.brand_name.strip():
            raise ValueError("brand_name: must not be empty")
        urls = [("brand_url", self.brand_url)] if self.brand_url else []
        for link in self.brand_links:
            if not link.label.strip():
                raise ValueError("brand_links: every link needs a label")
            urls.append((f"brand_links ({link.label})", link.url))
        for name, url in urls:
            if not url.lower().startswith(LINK_SCHEMES):
                raise ValueError(f"{name}: URL must start with {', '.join(LINK_SCHEMES)}")

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
        return str(raw)
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
        if env is not None and name not in {"locations", "brand_links"}:
            values[name] = _coerce(env, hint, name)

    return Settings(**values)
