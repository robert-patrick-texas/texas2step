"""Print the one-page sheet, on US Letter and on A4, from scripts/at-a-glance.html.

    make sheet      # uv run --with playwright python scripts/build_sheet.py

It writes docs/at-a-glance.pdf (US Letter) and docs/at-a-glance-a4.pdf (A4). It needs
Playwright's Chromium once: uv run --with playwright playwright install chromium, and the
fonts that docs/development.md names. The sheet is docs/at-a-glance.md condensed onto one
page, so change both together, rebuild, and commit the PDFs. The build fails, and leaves
both PDFs as they were, if the sheet runs over one page on either paper. The footer carries
the version from src/texas2step/__init__.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(__file__).with_name("at-a-glance.html")
# Each paper, as CSS names it for @page, and its PDF.
SHEETS = {
    "letter": ROOT / "docs" / "at-a-glance.pdf",
    "A4": ROOT / "docs" / "at-a-glance-a4.pdf",
}


def version() -> str:
    text = (ROOT / "src" / "texas2step" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__ = "(.+)"$', text, re.MULTILINE)
    assert found, "no __version__ in src/texas2step/__init__.py"
    return found.group(1)


def main() -> int:
    pdfs: dict[str, bytes] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(SOURCE.as_uri())
        page.evaluate("v => { document.getElementById('version').textContent = v }", version())
        for paper in SHEETS:
            # A later @page rule wins, so each paper's replaces the one before.
            page.add_style_tag(content=f"@page {{ size: {paper}; }}")
            pdfs[paper] = page.pdf(prefer_css_page_size=True, print_background=True, tagged=True)
        browser.close()
    for paper, pdf in pdfs.items():
        # Chromium writes each page's dictionary in the clear.
        pages = len(re.findall(rb"/Type\s*/Page\b", pdf))
        if pages != 1:
            print(f"{SOURCE.name} prints on {pages} {paper} pages, not one", file=sys.stderr)
            return 1
    for paper, pdf in pdfs.items():
        out = SHEETS[paper]
        out.write_bytes(pdf)
        print(f"{out.relative_to(ROOT)}: one {paper} page, {len(pdf) // 1024} KiB", file=sys.stderr)
    # Both papers embed the same fonts.
    if not re.search(rb"/BaseFont\s*/\w+\+Inter-", pdf) or re.search(rb"/Subtype\s*/Type3", pdf):
        print("warning: Inter isn't embedded as TrueType; see docs/development.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
