"""DIARL — Build USER_GUIDE.pdf from docs/USER_GUIDE.md.

Pipeline:
1. Read docs/USER_GUIDE.md (CommonMark + GFM tables + fenced code).
2. Convert mermaid code-blocks into <div class="mermaid"> blocks.
3. Render the rest with the python-markdown library (extensions: tables,
   fenced_code, toc, codehilite).
4. Wrap in an HTML template that pulls Mermaid.js from CDN and applies a
   print-friendly stylesheet (serif body, monospace code, A4 margins,
   page breaks before each top-level <h2>).
5. Use a headless Chromium build (Chrome / Edge) to print the rendered
   page to PDF, allowing time for Mermaid to render.

Usage
-----
.. code-block:: bash

    python scripts/build_user_guide_pdf.py
    # produces build/USER_GUIDE.html (intermediate) and docs/USER_GUIDE.pdf

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-04-29
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import markdown


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>DIARL 操作手冊</title>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: false, theme: 'default', flowchart: {{ htmlLabels: true, curve: 'basis' }} }});
  (async () => {{
    await mermaid.run({{ querySelector: '.mermaid' }});
    window.__mermaidDone = true;
  }})();
</script>
<style>
  @page {{ size: A4; margin: 18mm 16mm 18mm 16mm; }}
  body {{
    font-family: "Source Han Serif TC", "Noto Serif CJK TC", "Microsoft JhengHei",
                 "PingFang TC", "Songti TC", Georgia, "Times New Roman", serif;
    font-size: 11pt;
    line-height: 1.55;
    color: #1f2328;
    max-width: 100%;
    margin: 0;
    padding: 0;
  }}
  h1, h2, h3, h4 {{
    font-family: "Source Han Sans TC", "Noto Sans CJK TC", "Microsoft JhengHei",
                 "Helvetica Neue", Arial, sans-serif;
    color: #0d6efd;
    margin-top: 1.2em;
    margin-bottom: 0.6em;
    page-break-after: avoid;
  }}
  h1 {{ font-size: 26pt; border-bottom: 3px solid #0d6efd; padding-bottom: 0.2em; }}
  h2 {{ font-size: 19pt; border-bottom: 1px solid #dee2e6; padding-bottom: 0.15em; page-break-before: always; }}
  h2:first-of-type {{ page-break-before: auto; }}
  h3 {{ font-size: 14pt; }}
  h4 {{ font-size: 12pt; }}
  p {{ margin: 0.6em 0; }}
  blockquote {{
    border-left: 4px solid #ffc107;
    background: #fff8e1;
    padding: 0.6em 1em;
    margin: 1em 0;
    color: #5f4500;
  }}
  code {{
    font-family: "JetBrains Mono", Consolas, "SF Mono", Menlo, monospace;
    font-size: 9.5pt;
    background: #f3f4f6;
    padding: 0.1em 0.35em;
    border-radius: 3px;
  }}
  pre {{
    background: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 0.8em 1em;
    overflow-x: auto;
    page-break-inside: avoid;
  }}
  pre code {{ background: transparent; padding: 0; font-size: 9pt; }}
  table {{
    border-collapse: collapse;
    margin: 1em 0;
    width: 100%;
    font-size: 10pt;
    page-break-inside: avoid;
  }}
  th, td {{
    border: 1px solid #d0d7de;
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
  }}
  th {{ background: #f6f8fa; font-weight: 600; }}
  hr {{ border: none; border-top: 1px solid #dee2e6; margin: 2em 0; }}
  .mermaid {{
    text-align: center;
    margin: 1.2em 0;
    page-break-inside: avoid;
  }}
  ul, ol {{ margin: 0.5em 0; padding-left: 1.6em; }}
  li {{ margin: 0.2em 0; }}
  a {{ color: #0d6efd; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _replace_mermaid_blocks(md_text: str) -> str:
    """Convert ```mermaid fenced blocks into raw <div class="mermaid">...</div>.

    python-markdown does not know about Mermaid. We hide each block from the
    Markdown parser by emitting an HTML island, which the parser passes
    through verbatim when wrapped between blank lines.
    """
    pattern = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)

    def repl(m: re.Match[str]) -> str:
        body = m.group(1).rstrip()
        return f'\n\n<div class="mermaid">\n{body}\n</div>\n\n'

    return pattern.sub(repl, md_text)


def _find_chrome() -> str | None:
    """Return path to a Chromium-based browser executable, or None."""
    candidates = [
        os.environ.get("CHROME"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    src = repo / "docs" / "USER_GUIDE.md"
    if not src.exists():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 1

    build = repo / "build"
    build.mkdir(exist_ok=True)
    html_path = build / "USER_GUIDE.html"
    # PDF goes to docs/ so it sits alongside the .md and .docx as a tracked
    # deliverable; HTML stays in build/ as an intermediate (gitignored).
    pdf_path = repo / "docs" / "USER_GUIDE.pdf"

    md_text = src.read_text(encoding="utf-8")
    md_text = _replace_mermaid_blocks(md_text)

    body_html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "md_in_html"],
        output_format="html5",
    )
    html_doc = HTML_TEMPLATE.format(body=body_html)
    html_path.write_text(html_doc, encoding="utf-8")
    print(f"[build] wrote {html_path.relative_to(repo)} ({len(html_doc):,} chars)")

    chrome = _find_chrome()
    if chrome is None:
        print("ERROR: no Chromium-based browser found in PATH or default locations.", file=sys.stderr)
        print("Set the CHROME environment variable to chrome.exe / msedge.exe.", file=sys.stderr)
        return 2

    print(f"[build] using browser: {chrome}")

    # Prefer playwright when available so we can synchronously wait for
    # mermaid to finish rendering before printing.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chrome,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.goto(html_path.as_uri(), wait_until="networkidle")
            try:
                page.wait_for_function("() => window.__mermaidDone === true", timeout=30000)
                print("[build] mermaid render complete")
            except Exception as exc:
                print(f"[build][warn] mermaid wait timed out: {exc}")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
                print_background=True,
                prefer_css_page_size=True,
            )
            browser.close()
    except ModuleNotFoundError:
        print("[build] playwright unavailable, falling back to chrome --print-to-pdf")
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            "--virtual-time-budget=15000",
            f"--print-to-pdf={pdf_path}",
            f"file:///{html_path.as_posix()}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            return proc.returncode

    if not pdf_path.exists():
        print("ERROR: PDF was not produced", file=sys.stderr)
        return 3

    size_kb = pdf_path.stat().st_size / 1024
    print(f"[build] wrote {pdf_path.relative_to(repo)} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
