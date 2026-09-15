"""
Render docs/architecture.mmd to an SVG, mechanically.

    make diagram

WHY SVG AND NOT PNG
-------------------
An SVG is text. It diffs, it scales, and GitHub renders it inline -- so the
committed artifact is readable in a pull request instead of appearing as
`Bin 214382 -> 219117 bytes`, which is a review that cannot happen.

WHY RENDERED RATHER THAN DRAWN
------------------------------
docs/architecture.mmd is the source of truth and a test asserts every model and
dataset in it against the dbt manifest. That check is worth nothing if the image
beside it was drawn separately, because then the verified thing and the displayed
thing are two different artifacts and only one of them is checked. Rendering the
image FROM the source closes that: there is one description of the architecture,
and the picture is a projection of it.

Uses the Selenium/Chrome that scripts/capture_demo_screenshot.py already needs,
rather than mermaid-cli, which pulls its own ~150 MB Chrome via puppeteer.
Mermaid itself comes from a CDN, so this needs network; it is a docs tool, not
part of any build or test path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "architecture.mmd"
DEFAULT_OUT = ROOT / "docs" / "img" / "architecture.svg"

MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body{{margin:0;padding:0;background:transparent}}
  #out{{padding:16px}}
</style></head>
<body><div id="out"></div>
<script type="module">
  import mermaid from "{cdn}";
  mermaid.initialize({{
    startOnLoad: false,
    theme: "dark",
    securityLevel: "loose",
    flowchart: {{ htmlLabels: true, curve: "basis", nodeSpacing: 45, rankSpacing: 55 }},
  }});
  const src = {source};
  try {{
    const {{ svg }} = await mermaid.render("diagram", src);
    document.getElementById("out").innerHTML = svg;
    window.__done = true;
  }} catch (e) {{
    window.__error = String(e && e.message ? e.message : e);
  }}
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    source = args.source.read_text(encoding="utf-8")
    scratch = ROOT / "docs" / "img" / "_render.html"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(
        PAGE.format(cdn=MERMAID, source=json.dumps(source)), encoding="utf-8", newline=""
    )

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=2200,2600")
    options.add_argument("--hide-scrollbars")
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(scratch.resolve().as_uri())

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if driver.execute_script("return window.__done === true"):
                break
            error = driver.execute_script("return window.__error || null")
            if error:
                # Mermaid's own parse error, which names the line. Far more
                # useful than a blank image, which is what a silent failure here
                # would commit.
                print(f"mermaid could not parse {args.source}:\n\n{error}", file=sys.stderr)
                return 1
            time.sleep(0.5)
        else:
            print(f"timed out after {args.timeout}s waiting for mermaid", file=sys.stderr)
            return 1

        svg = driver.execute_script("return document.querySelector('#out svg').outerHTML;")
        if not svg or len(svg) < 2000:
            print(
                f"render produced {len(svg or '')} bytes of SVG, which is not a diagram",
                file=sys.stderr,
            )
            return 1

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(svg, encoding="utf-8", newline="")
        print(f"wrote {args.out}  ({len(svg) // 1024} KB of SVG)")
        return 0
    finally:
        driver.quit()
        scratch.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
