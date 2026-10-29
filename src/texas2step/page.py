"""The web UI's HTML shell, filled in for this site and each request.

``static/index.html`` (built from ``web/public/index.html``) is a template. The server
fills in what only it knows:

- the path prefix it is published under (``<base href>``),
- the version (read by the UI from a meta tag),
- the site's default theme, and which pages and header controls it shows,
- the branding: name, logo, organization, links, notice, the organization's own footer
  and the footer's bottom line (organization, links, credit, tagline),
- whether this visitor's access code screen shows the code box alone.

Branding is written into the page itself rather than added by script, so a renamed site
never flashes the default name. Every value is HTML-escaped. Nothing from the request
reaches the page except the prefix, which ``ForwardedPrefix`` has already validated.
"""

from __future__ import annotations

import hashlib
import logging
import re
from html import escape
from pathlib import Path

from .config import FOOTER_LINK, PAGES, Settings

log = logging.getLogger(__name__)

DEFAULT_NAME = "texas2step"
PROJECT_URL = "https://robert-patrick-texas.github.io/texas2step/"
LOGO_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def logo_file(settings: Settings) -> Path | None:
    """The configured logo, if it is a readable image file; otherwise None, with a warning."""
    if not settings.brand_logo:
        return None
    path = Path(settings.brand_logo)
    if path.suffix.lower() not in LOGO_TYPES:
        log.warning("brand_logo %s: use one of %s", path, ", ".join(LOGO_TYPES))
    elif not path.is_file():
        log.warning("brand_logo %s: no such file", path)
    else:
        try:
            path.read_bytes()
            return path
        except OSError as exc:
            log.warning("brand_logo %s: %s", path, exc)
    return None


# The service desk's pages: with internal_networks set, only those networks get them.
DESK_PAGES = frozenset({"history", "api"})


class Shell:
    """Renders the page for this site. The site-wide parts are worked out once."""

    def __init__(self, settings: Settings, logo: Path | None) -> None:
        self.settings = settings
        self.logo_version = hashlib.sha256(logo.read_bytes()).hexdigest()[:10] if logo else None
        self.logo_type = LOGO_TYPES[logo.suffix.lower()] if logo else None

    @property
    def icon(self) -> str:
        """The browser tab's icon, relative to the site: the logo, or texas2step's star."""
        return f"brand/logo?v={self.logo_version}" if self.logo_version else "favicon.svg"

    def render(
        self,
        template: str,
        root_path: str,
        version: str,
        hide: frozenset[str] = frozenset(),
        gated: bool = False,
    ) -> str:
        """The page, without the tabs of ``hide`` (the desk's, for visitors from outside).
        ``gated``: this visitor needs the access code, unless their browser holds a pass."""
        s = self.settings
        pages = [p for p in s.pages if p not in hide]
        page = template.replace('<base href="/" />', f'<base href="{escape(root_path)}/" />', 1)
        page = page.replace(
            '<meta name="texas2step-version" content="" />',
            f'<meta name="texas2step-version" content="{escape(version)}" />',
            1,
        )
        root = ['lang="en"']
        if s.default_theme != "system":
            root.append(f'data-default-theme="{s.default_theme}"')
        # How long Monitor and Diag may run, where the site limits them (ui/runtime.ts).
        if s.page_monitor_max_runtime:
            root.append(f'data-monitor-max="{s.page_monitor_max_runtime}"')
        if s.page_diag_max_runtime:
            root.append(f'data-diag-max="{s.page_diag_max_runtime}"')
        # Without its picker, a visitor's saved theme or size no longer applies.
        if not s.header_theme:
            root.append("data-fixed-theme")
            page = _fill(page, "theme", "")
        if not s.header_size:
            root.append("data-fixed-size")
            page = _fill(page, "size", "")
        if s.access_card_only and gated:
            root.append("data-card-only")
        if pages != list(PAGES):
            # The tabs of hidden pages go, and the UI is told which pages it has.
            root.append(f'data-pages="{" ".join(pages)}"')
            for hidden in set(PAGES) - set(pages):
                page = re.sub(rf'\s*<a [^>]*data-page="{hidden}"[^>]*>[^<]*</a>', "", page, count=1)
        page = page.replace('<html lang="en">', f"<html {' '.join(root)}>", 1)
        if s.brand_name != DEFAULT_NAME:
            name = escape(s.brand_name)
            page = page.replace("<title>texas2step</title>", f"<title>{name}</title>", 1)
            page = page.replace('aria-label="texas2step home"', f'aria-label="{name} home"', 1)
            page = _fill(page, "name", f'<span class="brand-word">{name}</span>')
        if not s.header_logo:
            page = _fill(page, "mark", "")
        elif self.logo_version:
            mark = f'<img class="brand-mark" src="brand/logo?v={self.logo_version}" alt="" />'
            page = _fill(page, "mark", mark)
        if self.logo_version:  # the tab's icon, shown in the header or not
            page = page.replace(
                '<link rel="icon" href="favicon.svg" type="image/svg+xml" />',
                f'<link rel="icon" href="{self.icon}" type="{self.logo_type}" />',
                1,
            )
        if s.brand_notice:
            notice = f'<div class="notice" role="note">{escape(s.brand_notice)}</div>'
            page = _fill(page, "notice", notice)
        if org := self._organization("brand-org"):
            page = _fill(page, "org", org)
        links = [_link(link.label, link.url) for link in s.brand_links]
        if links:
            nav = f'<nav class="topbar-links" aria-label="Links">{"".join(links)}</nav>'
            page = _fill(page, "links", nav)
        return _fill(page, "footer", self._footer(links))

    def _footer(self, links: list[str]) -> str:
        """The organization's own footer, then the bottom line; nothing when both are off."""
        s = self.settings
        line = []
        if s.footer_line:
            line = [self._organization(), *links, self._credit()]
            if s.brand_tagline:
                line.append(f'<span class="footer-tagline">{escape(s.brand_tagline)}</span>')
        dot = '<span class="dot" aria-hidden="true">·</span>'
        body = self._own_footer() + dot.join(x for x in line if x)
        return f'<footer class="footer">{body}</footer>' if body else ""

    def _credit(self) -> str:
        """The footer's "texas2step <version>": a link to the project, plain text, or none.
        The page writes the version in."""
        s = self.settings
        if s.brand_credit == "none":
            return ""
        powered = (
            '<span class="footer-powered">Powered by</span>' if s.brand_name != DEFAULT_NAME else ""
        )
        if s.brand_credit == "text":
            return f'{powered}<span id="footer-version">texas2step</span>'
        new_tab = 'target="_blank" rel="noopener"'
        return f'{powered}<a id="footer-version" href="{PROJECT_URL}" {new_tab}>texas2step</a>'

    def _own_footer(self) -> str:
        """The organization's footer: its text on the left, its links on the right."""
        s = self.settings
        # A blank line starts a paragraph; each other line starts a line.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", s.brand_footer.strip()) if p.strip()]
        text = "".join(
            "<p>" + "<br />".join(_inline(line.strip()) for line in p.splitlines()) + "</p>"
            for p in paragraphs
        )
        links = "".join(f"<li>{_link(link.label, link.url)}</li>" for link in s.brand_footer_links)
        parts = []
        if text:
            parts.append(f'<div class="own-footer-text">{text}</div>')
        if links:
            parts.append(
                f'<nav class="own-footer-links" aria-label="Footer links"><ul>{links}</ul></nav>'
            )
        if not parts:
            return ""
        body = f'<div class="own-footer-body{" solo" if len(parts) == 1 else ""}">'
        # The outer div is a row of its own, above the credit line.
        return f'<div class="own-footer">{body}{"".join(parts)}</div></div>'

    def _organization(self, cls: str = "") -> str:
        s = self.settings
        label = s.brand_organization or re.sub(r"^\w+:(//)?", "", s.brand_url).rstrip("/")
        if not label:
            return ""
        if s.brand_url:
            return _link(label, s.brand_url, cls)
        return f"<span{_class(cls)}>{escape(label)}</span>"


def _class(cls: str) -> str:
    return f' class="{cls}"' if cls else ""


def _link(label: str, url: str, cls: str = "") -> str:
    new_tab = ' target="_blank" rel="noopener"' if url.lower().startswith("http") else ""
    return f'<a{_class(cls)} href="{escape(url)}"{new_tab}>{escape(label)}</a>'


def _inline(text: str) -> str:
    """Plain text, with each [label](url) in it made a link."""
    out, last = [], 0
    for m in FOOTER_LINK.finditer(text):
        out += [escape(text[last : m.start()]), _link(m.group(1), m.group(2))]
        last = m.end()
    return "".join([*out, escape(text[last:])])


def _fill(page: str, slot: str, html: str) -> str:
    """Put ``html`` in a slot: ``<!--t2s:slot-->`` or ``<!--t2s:slot-->…<!--/t2s:slot-->``."""
    pattern = re.compile(rf"<!--t2s:{slot}-->(?:.*?<!--/t2s:{slot}-->)?", re.S)
    return pattern.sub(lambda _: html, page, count=1)
