#!/usr/bin/env python3
"""Neutral, ATS-friendly resume PDF renderer.

Deliberately unbranded: no colours, no logos, no gradients. Designed to read
like a serious senior resume that a recruiter scans and hands to a hiring
manager. ATS parsers strip styling but the semantic structure (h1/h2/lists/
strong) survives.

Usage:
    python render_resume.py <input.md> [output.pdf]
        # output defaults to input.pdf next to the markdown
"""

from __future__ import annotations

import sys
from pathlib import Path

import markdown
from weasyprint import CSS, HTML

# ── Style ─────────────────────────────────────────────────────────────────────
# Conservative resume aesthetic: clean sans, generous leading, black-on-white,
# subtle horizontal rule between sections.

CSS_TEXT = r"""
@page {
    size: Letter;
    margin: 0.55in 0.65in 0.55in 0.65in;
}

* { box-sizing: border-box; }

html, body {
    font-family: "Calibri", "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.38;
    color: #111111;
    background: #ffffff;
    margin: 0;
    padding: 0;
}

/* Name / contact header */
h1:first-of-type {
    font-size: 22pt;
    font-weight: 700;
    letter-spacing: 0.2px;
    margin: 0 0 2pt 0;
    color: #0b0b0b;
    border: none;
    padding: 0;
}

/* Tagline / contact line directly under the name */
h1:first-of-type + p,
h1:first-of-type + p + p {
    font-size: 10pt;
    color: #333333;
    margin: 0 0 2pt 0;
}

/* Section headers (## Summary, ## Experience, etc.) */
h2 {
    font-size: 11.5pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: #0b0b0b;
    margin: 14pt 0 4pt 0;
    padding-bottom: 2pt;
    border-bottom: 1px solid #777777;
}

/* Sub-sections (### Role @ Company) */
h3 {
    font-size: 10.5pt;
    font-weight: 700;
    color: #111111;
    margin: 8pt 0 1pt 0;
}

h4 {
    font-size: 10pt;
    font-weight: 600;
    color: #222222;
    margin: 6pt 0 1pt 0;
}

p {
    margin: 0 0 4pt 0;
}

ul {
    margin: 2pt 0 6pt 0;
    padding-left: 16pt;
}

ul ul {
    margin: 1pt 0 1pt 0;
}

li {
    margin: 0 0 1.5pt 0;
}

strong, b {
    font-weight: 700;
    color: #0b0b0b;
}

em, i {
    font-style: italic;
    color: #222222;
}

a, a:visited {
    color: #0b0b0b;
    text-decoration: none;
}

/* The horizontal rules in the markdown act as section dividers. We let h2
   borders do that work; hide the rules to keep the page calm. */
hr {
    display: none;
}

/* Explicit page-break marker.  Use <div class="page-break"></div> in the
   markdown wherever you want a hard break (e.g. after Core Competencies). */
.page-break {
    page-break-after: always;
    break-after: page;
    height: 0;
    margin: 0;
}

/* Tables (skills matrix etc.) */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 4pt 0 6pt 0;
    font-size: 10pt;
}

th, td {
    text-align: left;
    padding: 3pt 6pt 3pt 0;
    vertical-align: top;
    border: none;
}

th {
    font-weight: 700;
    border-bottom: 1px solid #999999;
}

td + td, th + th {
    padding-left: 10pt;
}

code {
    font-family: "Consolas", "Menlo", monospace;
    font-size: 9.5pt;
    background: transparent;
    color: #111111;
}

blockquote {
    margin: 4pt 0 4pt 12pt;
    padding-left: 8pt;
    border-left: 2px solid #cccccc;
    color: #333333;
    font-style: italic;
}

/* Avoid orphaned headings */
h2, h3, h4 {
    page-break-after: avoid;
    break-after: avoid;
}

/* Keep company line + description glued to the role heading */
h3 + p,
h3 + p + p,
h3 + p + ul {
    page-break-before: avoid;
    break-before: avoid;
}

ul, li {
    page-break-inside: avoid;
    break-inside: avoid;
}
"""


def render(input_md: Path, output_pdf: Path) -> None:
    md_text = input_md.read_text(encoding="utf-8")

    html_body = markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists", "smarty"],
        output_format="html5",
    )

    full_html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'></head>"
        f"<body>{html_body}</body></html>"
    )

    HTML(string=full_html, base_url=str(input_md.parent)).write_pdf(
        target=str(output_pdf),
        stylesheets=[CSS(string=CSS_TEXT)],
    )
    print(f"wrote {output_pdf}  ({output_pdf.stat().st_size:,} bytes)")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = Path(argv[1]).resolve()
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        return 1
    dst = Path(argv[2]).resolve() if len(argv) >= 3 else src.with_suffix(".pdf")
    render(src, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
