"""Render every Markdown file in the repository into the static documentation site.

    uv run python scripts/build_docs.py            # write html/
    uv run python scripts/build_docs.py --watch    # and rebuild whenever a source changes

README.md becomes index.html in its directory and every other X.md becomes X.html. Each
page carries the same left-hand navigation and a link to the GitHub repository. Links
between Markdown files are rewritten to the matching pages, links to other repository
files point at GitHub, and images are copied alongside. An internal link or anchor that
does not resolve fails the build, so CI catches documentation rot.

The output uses relative URLs only and no JavaScript, so it works on GitHub Pages, behind
a web server under any path, and straight from the file system.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import posixpath
import re
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

ROOT = Path(__file__).resolve().parent.parent
CSS = Path(__file__).with_name("docs.css")
FAVICON = ROOT / "web" / "public" / "favicon.svg"
BRANCH = "main"
SKIP_DIRS = {"node_modules", "html", "dist", "build", "stage", "staging", "__pycache__"}

# Navigation order and labels (None: the page's first heading). Pages not listed here sit
# between the two groups, alphabetically.
LEADING: list[tuple[str, str | None]] = [
    ("README.md", "Overview"),
    ("docs/install.md", "Install"),
    ("docs/configuration.md", None),
    ("docs/methodology.md", None),
    ("docs/api.md", None),
    ("docs/development.md", None),
]
TRAILING: list[tuple[str, str | None]] = [
    ("LICENSE.md", "License"),
    ("legacy/README.md", "Legacy prototype"),
]

GITHUB_MARK = (
    '<svg class="mark" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
    '<path fill="currentColor" d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08'
    "-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 "
    "0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 "
    "0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 "
    "0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23"
    "-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 "
    '.21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>'
)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="{root}assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{root}assets/docs.css?v={css_version}">
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="{home}">{logo}<span class="brand-name">texas2step</span>\
<span class="brand-tag">docs</span></a>
    <a class="repo" href="{repo}">{github}<span class="wide">Source on GitHub</span>\
<span class="narrow">GitHub</span></a>
  </div>
</header>
<div class="layout">
  <nav class="sidebar" aria-label="Documentation">
{nav}
  </nav>
  <main id="content" class="doc">
    <article class="prose">
{body}    </article>
    <footer class="page-foot">
      <a href="{source}">View {src} on GitHub</a>
      <span>texas2step is open source under the {license}.</span>
    </footer>
  </main>
</div>
</body>
</html>
"""


def output_path(src: PurePosixPath) -> PurePosixPath:
    """README.md is its directory's index; everything else keeps its name as .html."""
    if src.name == "README.md":
        return src.parent / "index.html"
    return src.with_suffix(".html")


def relative(target: PurePosixPath, start: PurePosixPath) -> str:
    return posixpath.relpath(str(target), str(start))


def slugify(text: str) -> str:
    """GitHub's heading anchors (github-slugger): lower case, punctuation dropped."""
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def plain(inline: Token) -> str:
    """The visible text of an inline token, without markup."""
    return "".join(
        child.content for child in inline.children or [] if child.type in ("text", "code_inline")
    ).strip()


def highlight_code(code: str, lang: str, _attrs: str) -> str:
    try:
        lexer = get_lexer_by_name(lang or "text")
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def render_heading_close(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
    token = tokens[idx]
    anchor = token.meta.get("anchor")
    link = ""
    if anchor and token.tag != "h1":
        link = f'<a class="anchor" href="#{anchor}" aria-label="Link to this section">#</a>'
    return f"{link}</{token.tag}>\n"


def render_table_open(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
    # The wrapper scrolls a wide table sideways, so the table itself can fill the column.
    return '<div class="table-wrap">\n' + self.renderToken(tokens, idx, options, env)


def render_table_close(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
    return self.renderToken(tokens, idx, options, env) + "</div>\n"


def discover(root: Path) -> list[PurePosixPath]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        found += [
            PurePosixPath(Path(dirpath, name).relative_to(root).as_posix())
            for name in filenames
            if name.endswith(".md")
        ]
    return found


def repository_url(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    return project["urls"]["Repository"].rstrip("/")


@dataclass
class Page:
    src: PurePosixPath
    label: str = ""
    title: str = ""
    description: str = ""
    body: str = ""
    ids: set[str] = field(default_factory=set)
    sections: list[tuple[str, str]] = field(default_factory=list)

    @property
    def out(self) -> PurePosixPath:
        return output_path(self.src)

    @property
    def root(self) -> str:
        return "../" * (len(self.out.parts) - 1)


class Site:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = repository_url(root)
        self.md = MarkdownIt("gfm-like", {"highlight": highlight_code})
        # Like GitHub, link bare URLs only when they carry a scheme: "app.py" is not a site.
        if self.md.linkify is not None:
            self.md.linkify.set({"fuzzy_link": False})
        self.md.add_render_rule("heading_close", render_heading_close)
        self.md.add_render_rule("table_open", render_table_open)
        self.md.add_render_rule("table_close", render_table_close)

        labels = dict(LEADING + TRAILING)
        found = set(discover(root))
        lead = [p for p, _ in LEADING if PurePosixPath(p) in found]
        tail = [p for p, _ in TRAILING if PurePosixPath(p) in found]
        middle = sorted(found - {PurePosixPath(p) for p in lead + tail})
        self.lead = [PurePosixPath(p) for p in lead] + middle
        self.tail = [PurePosixPath(p) for p in tail]
        self.pages = {
            src: Page(src, label=labels.get(str(src)) or "") for src in self.lead + self.tail
        }
        self.assets: set[PurePosixPath] = set()
        self.anchors: list[tuple[Page, PurePosixPath, str, str]] = []
        self.problems: list[str] = []

    # ---- rendering -------------------------------------------------------------------

    def render(self) -> None:
        for page in self.pages.values():
            tokens = self.md.parse((self.root / page.src).read_text(encoding="utf-8"))
            self._annotate(page, tokens)
            page.body = self.md.renderer.render(tokens, self.md.options, {})
            page.label = page.label or page.title or page.src.stem
        for page, target, fragment, href in self.anchors:
            if fragment not in self.pages[target].ids:
                self.problems.append(f"{page.src}: no heading for #{fragment} in {target} ({href})")

    def _annotate(self, page: Page, tokens: list[Token]) -> None:
        seen: set[str] = set()
        for i, token in enumerate(tokens):
            if token.type == "heading_open":
                text = plain(tokens[i + 1])
                base = slug = slugify(text)
                n = 0
                while slug in seen:
                    n += 1
                    slug = f"{base}-{n}"
                seen.add(slug)
                page.ids.add(slug)
                token.attrSet("id", slug)
                tokens[i + 2].meta["anchor"] = slug
                if token.tag == "h1" and not page.title:
                    page.title = text
                elif token.tag == "h2":
                    page.sections.append((slug, text))
            elif token.type == "paragraph_open" and not page.description:
                page.description = " ".join(plain(tokens[i + 1]).split())[:200]
            elif token.type == "inline":
                for child in token.children or []:
                    if child.type == "link_open":
                        child.attrSet("href", self._link(page, str(child.attrGet("href"))))
                    elif child.type == "image":
                        child.attrSet("src", self._link(page, str(child.attrGet("src")), True))

    def _link(self, page: Page, href: str, image: bool = False) -> str:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            return href
        fragment = parts.fragment
        if not parts.path:
            if fragment:
                self.anchors.append((page, page.src, fragment, href))
            return href

        path = unquote(parts.path)
        joined = path.lstrip("/") if path.startswith("/") else posixpath.join(page.src.parent, path)
        target = PurePosixPath(posixpath.normpath(joined))
        if target.parts and target.parts[0] == "..":
            self.problems.append(f"{page.src}: {href} points outside the repository")
            return href
        on_disk = self.root / target
        suffix = f"#{fragment}" if fragment else ""

        if image:
            if on_disk.is_file():
                self.assets.add(target)
                return quote(relative(target, page.out.parent))
            self.problems.append(f"{page.src}: image {href} does not exist")
            return href
        if on_disk.is_dir() and target / "README.md" in self.pages:
            target = target / "README.md"
        if target in self.pages:
            if fragment:
                self.anchors.append((page, target, fragment, href))
            return quote(relative(self.pages[target].out, page.out.parent)) + suffix
        if on_disk.is_file():
            return f"{self.repo}/blob/{BRANCH}/{quote(str(target))}{suffix}"
        if on_disk.is_dir():
            return f"{self.repo}/tree/{BRANCH}/{quote(str(target))}"
        self.problems.append(f"{page.src}: link {href} does not resolve")
        return href

    # ---- output ----------------------------------------------------------------------

    def _nav(self, current: Page) -> str:
        def items(sources: list[PurePosixPath]) -> str:
            out = []
            for src in sources:
                page = self.pages[src]
                href = quote(relative(page.out, current.out.parent))
                mark = ' aria-current="page"' if page is current else ""
                toc = ""
                if page is current and page.sections:
                    links = "".join(
                        f'<li><a href="#{sid}">{html.escape(text)}</a></li>'
                        for sid, text in page.sections
                    )
                    toc = f'<ul class="toc">{links}</ul>'
                out.append(f'<li><a href="{href}"{mark}>{html.escape(page.label)}</a>{toc}</li>')
            return "".join(out)

        nav = f'    <ul class="pages">{items(self.lead)}</ul>'
        if self.tail:
            nav += (
                f'\n    <p class="group">Project</p>\n    <ul class="pages">{items(self.tail)}</ul>'
            )
        return nav

    def write(self, out: Path) -> None:
        css = CSS.read_text(encoding="utf-8")
        logo = (
            FAVICON.read_text(encoding="utf-8")
            .strip()
            .replace('id="g"', 'id="t2s-sun"')
            .replace("url(#g)", "url(#t2s-sun)")
            .replace("<svg ", '<svg class="logo" aria-hidden="true" focusable="false" ', 1)
        )
        license_src = PurePosixPath("LICENSE.md")
        rendered = {}
        for page in self.pages.values():
            if license_src in self.pages:
                href = quote(relative(self.pages[license_src].out, page.out.parent))
                license_link = f'<a href="{href}">Apache License 2.0</a>'
            else:
                license_link = "Apache License 2.0"
            title = (
                "texas2step documentation"
                if page.src == PurePosixPath("README.md")
                else f"{page.title or page.label} · texas2step docs"
            )
            rendered[page.out] = PAGE.format(
                title=html.escape(title),
                description=html.escape(page.description, quote=True),
                root=page.root,
                css_version=hashlib.sha256(css.encode()).hexdigest()[:10],
                home=quote(relative(PurePosixPath("index.html"), page.out.parent)),
                logo=logo,
                repo=html.escape(self.repo),
                github=GITHUB_MARK,
                nav=self._nav(page),
                body=page.body,
                source=html.escape(f"{self.repo}/blob/{BRANCH}/{quote(str(page.src))}"),
                src=html.escape(str(page.src)),
                license=license_link,
            )

        # Everything is rendered before html/ is touched, so a failure leaves it intact.
        # The directory itself stays: on the review server it is a symlink target.
        out.mkdir(parents=True, exist_ok=True)
        for child in out.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for path, text in rendered.items():
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_text(text, encoding="utf-8")
        (out / "assets").mkdir(exist_ok=True)
        (out / "assets" / "docs.css").write_text(css, encoding="utf-8")
        shutil.copyfile(FAVICON, out / "assets" / "favicon.svg")
        for asset in self.assets:
            (out / asset).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / asset, out / asset)
        # A .nojekyll marker makes GitHub Pages publish the files exactly as written.
        (out / ".nojekyll").write_text("")


def build(out: Path) -> tuple[bool, set[Path]]:
    """Render the site into ``out``. Returns success and the files it was built from."""
    site = Site(ROOT)
    resolved = out.resolve()
    if any((ROOT / src).resolve().is_relative_to(resolved) for src in site.pages):
        sys.exit(f"refusing to write into {out}: it contains the Markdown sources")
    site.render()
    site.write(out)
    inputs = {ROOT / src for src in site.pages} | {ROOT / a for a in site.assets}
    print(f"{out}: {len(site.pages)} pages, {len(site.assets)} images", file=sys.stderr)
    for problem in site.problems:
        print(f"  error: {problem}", file=sys.stderr)
    return not site.problems, inputs | {CSS, FAVICON, ROOT / "pyproject.toml"}


def watch(out: Path, inputs: set[Path]) -> None:
    script = Path(__file__).resolve()

    def snapshot() -> dict[Path, float]:
        files = inputs | {ROOT / src for src in discover(ROOT)} | {script}
        return {f: f.stat().st_mtime for f in files if f.exists()}

    print("watching for changes (Ctrl-C to stop)", file=sys.stderr)
    seen = snapshot()
    while True:
        time.sleep(1)
        now = snapshot()
        if now == seen:
            continue
        if now.get(script) != seen.get(script):
            os.execv(sys.executable, [sys.executable, *sys.argv])
        _, inputs = build(out)
        seen = snapshot()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "html", help="output directory")
    parser.add_argument("--watch", action="store_true", help="rebuild when a source changes")
    args = parser.parse_args(argv)
    ok, inputs = build(args.out)
    if args.watch:
        try:
            watch(args.out, inputs)
        except KeyboardInterrupt:
            return 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
