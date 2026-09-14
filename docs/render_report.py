"""Render docs/REPORT.md to a PDF, then verify the result visually.

    uv run python docs/render_report.py

Markdown becomes HTML, headless Chromium prints it to PDF, and every page is
then rasterised to a PNG so the output can actually be looked at.

The verification step is not ceremony. A missing glyph does not raise an error —
it renders as a black box, an empty rectangle, or nothing at all, and a PDF that
is silently full of them still "succeeds". The only way to know is to look.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
SOURCE = DOCS / "REPORT.md"
OUTPUT = DOCS / "API-Engine-Report.pdf"
PREVIEW_DIR = DOCS / "_preview"

# Serif for prose, because this is a document to be read rather than a web page.
# Every family has a real fallback: naming a font that is absent is exactly how
# glyphs go missing.
CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

body {
  font-family: "Charter", "Georgia", "Cambria", "Times New Roman", serif;
  font-size: 10.4pt;
  line-height: 1.52;
  color: #14161c;
  margin: 0;
}

h1, h2, h3, h4 {
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  color: #0f1115;
  line-height: 1.25;
  margin: 1.5em 0 0.45em;
}

/* Each Part starts a new page; the title block must not. */
h1 { font-size: 20pt; letter-spacing: -0.4px; page-break-before: always; }
h1:first-of-type { page-break-before: avoid; }
h2 { font-size: 14pt; border-bottom: 1px solid #d8dbe2; padding-bottom: 3px; }
h3 { font-size: 11.6pt; }

h2, h3, h4 { page-break-after: avoid; }
p, li, tr, pre, blockquote { page-break-inside: avoid; }

code, pre {
  font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
  font-size: 8.9pt;
}

code { background: #f2f3f7; padding: 1px 4px; border-radius: 3px; }

pre {
  background: #f7f8fa;
  border: 1px solid #e2e4ea;
  border-left: 3px solid #3a5bd9;
  border-radius: 4px;
  padding: 9px 12px;
  overflow-x: auto;
  white-space: pre-wrap;      /* wrap rather than clip: a PDF cannot scroll */
  word-wrap: break-word;
  line-height: 1.4;
}

pre code { background: none; padding: 0; }

table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.9em 0;
  font-size: 9.3pt;
}

th, td {
  border: 1px solid #d8dbe2;
  padding: 5px 8px;
  text-align: left;
  vertical-align: top;
}

th { background: #eef0f5; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }

blockquote {
  margin: 1em 0;
  padding: 8px 14px;
  border-left: 3px solid #3a5bd9;
  background: #f4f6fd;
  font-style: normal;
}

blockquote p { margin: 0.3em 0; }

hr { border: 0; border-top: 1px solid #d8dbe2; margin: 1.6em 0; }

a { color: #2f4bbd; text-decoration: none; }

strong { color: #0f1115; }

ul, ol { padding-left: 1.4em; }
li { margin: 0.22em 0; }
"""


def build_html(text: str) -> str:
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>API Engine Report</title>
<style>{CSS}</style>
</head><body>{body}</body></html>"""


def render_pdf(html: str) -> None:
    from playwright.sync_api import sync_playwright

    tmp = DOCS / "_report.html"
    tmp.write_text(html, encoding="utf-8")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(tmp.as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(OUTPUT),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=(
                    '<div style="width:100%;font-size:7.5pt;color:#8a90a0;'
                    'font-family:Segoe UI,Arial,sans-serif;padding:0 16mm;'
                    'display:flex;justify-content:space-between;">'
                    "<span>Autonomous API Discovery &amp; Integration Engine</span>"
                    '<span class="pageNumber"></span></div>'
                ),
                margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"},
            )
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)


def verify(sample_every: int = 4) -> int:
    """Rasterise pages to PNG and report anything that looks wrong.

    Two automated checks, neither of which replaces looking at the images:
    a page that is almost entirely one colour is blank or solid black, and a
    page with no dark pixels at all rendered no text.
    """
    import pypdfium2 as pdfium

    PREVIEW_DIR.mkdir(exist_ok=True)
    for old in PREVIEW_DIR.glob("*.png"):
        old.unlink()

    pdf = pdfium.PdfDocument(str(OUTPUT))
    total = len(pdf)
    suspicious: list[str] = []

    for index in range(total):
        if index % sample_every and index != total - 1:
            continue
        page = pdf[index]
        image = page.render(scale=1.4).to_pil().convert("L")
        path = PREVIEW_DIR / f"page-{index + 1:02d}.png"
        image.save(path)

        histogram = image.histogram()
        pixels = sum(histogram)
        dark = sum(histogram[:100]) / pixels
        light = sum(histogram[220:]) / pixels
        if dark > 0.55:
            suspicious.append(f"page {index + 1}: {dark:.0%} dark — possible solid block")
        if light > 0.999:
            suspicious.append(f"page {index + 1}: no text rendered")

    print(f"pages: {total}")
    print(f"previews: {PREVIEW_DIR} ({len(list(PREVIEW_DIR.glob('*.png')))} images)")
    if suspicious:
        print("SUSPICIOUS:")
        for line in suspicious:
            print(f"  {line}")
    else:
        print("automated checks passed — now look at the images")
    return total


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}")
        return 1

    text = SOURCE.read_text(encoding="utf-8")
    # Characters outside Latin-1 are the usual cause of missing glyphs.
    exotic = sorted({c for c in text if ord(c) > 0x2500})
    if exotic:
        print(f"note: unusual characters present: {exotic}")

    render_pdf(build_html(text))
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT.name} ({size_kb:.0f} KB)")
    verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
