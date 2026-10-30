"""Print docs/at-a-glance.pdf, the one-page sheet, from scripts/at-a-glance.html.

    make sheet      # uv run --with playwright python scripts/build_sheet.py

It needs Playwright's Chromium once: uv run --with playwright playwright install chromium,
and the fonts that docs/development.md names. The sheet is docs/at-a-glance.md condensed
onto one US Letter page, so change both together, rebuild, and commit the PDF. The build
fails, and leaves the PDF as it was, if the sheet runs over one page. The footer carries
the version from src/texas2step/__init__.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(__file__).with_name("at-a-glance.html")
OUT = ROOT / "docs" / "at-a-glance.pdf"


def version() -> str:
    text = (ROOT / "src" / "texas2step" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__ = "(.+)"$', text, re.MULTILINE)
    assert found, "no __version__ in src/texas2step/__init__.py"
    return found.group(1)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(SOURCE.as_uri())
        page.evaluate("v => { document.getElementById('version').textContent = v }", version())
        pdf = page.pdf(prefer_css_page_size=True, print_background=True, tagged=True)
        browser.close()
    # Chromium writes each page's dictionary in the clear.
    pages = len(re.findall(rb"/Type\s*/Page\b", pdf))
    if pages != 1:
        print(f"{SOURCE.name} prints on {pages} pages, not one", file=sys.stderr)
        return 1
    OUT.write_bytes(pdf)
    print(f"{OUT.relative_to(ROOT)}: one page, {len(pdf) // 1024} KiB", file=sys.stderr)
    if not re.search(rb"/BaseFont\s*/\w+\+Inter-", pdf) or re.search(rb"/Subtype\s*/Type3", pdf):
        print("warning: Inter isn't embedded as TrueType; see docs/development.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
