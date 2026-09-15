"""
Capture the README's NL->SQL screenshot by driving the real app.

    make screenshot          # starts nothing; expects the app already running
    python scripts/capture_demo_screenshot.py --url http://localhost:8501

WHY THIS IS A SCRIPT AND NOT A SAVED PNG
----------------------------------------
A screenshot is the one artifact in this repo that cannot be diffed, cannot be
regenerated, and does not fail a test when it stops being true. Every other
figure the README prints comes from something that runs. An image pasted in once
is a claim about the app frozen at a moment nobody can reproduce -- which is the
same shape as the hand-typed figures this project already replaced twice.

So the image is generated: this drives the actual Streamlit app, asks a real
question, waits for a real answer, and crops the result. Re-run it and the
README's screenshot is current, or it fails and says why.

WHAT IT ASSERTS BEFORE SAVING
-----------------------------
A screenshot that silently captures an error state is worse than no screenshot,
so the capture is refused unless the panel shows all three things the README
claims it shows: the generated SQL, the per-question cost, and the LIMIT the
guard injected. The last one is the reason the question below is phrased without
a "top N" -- asked for a ranking, the model writes its own LIMIT, the guard has
nothing to add, and the caption the README points at does not appear.

This spends one live Gemini call, about $0.0006, against the demo's own ledger.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "img" / "nl2sql-demo.png"

# Deliberately not a "top N" question: see the module docstring. Phrased the way
# a visitor would, because the screenshot is evidence about the demo and a
# question tuned to make the agent look good is not evidence.
QUESTION = "what is the delivery complaint rate for each customer segment?"

REQUIRED = [
    ("the generated SQL", "SELECT"),
    ("the per-question cost", "of Gemini"),
    ("the guard's injected LIMIT", "LIMIT added by the guard"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8501")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=1600)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={args.width},{args.height}")
    options.add_argument("--force-device-scale-factor=2")  # legible at README width
    options.add_argument("--hide-scrollbars")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(args.url)
        wait = WebDriverWait(driver, args.timeout)
        wait.until(
            ec.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="stAppViewContainer"]'))
        )

        box = wait.until(
            ec.element_to_be_clickable((By.CSS_SELECTOR, 'input[aria-label="Question"]'))
        )
        box.clear()
        box.send_keys(QUESTION)
        box.send_keys(Keys.RETURN)

        # The answer is a live model call behind two budgets; poll for the
        # caption rather than sleeping a guessed interval.
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if "LIMIT added by the guard" in driver.find_element(By.TAG_NAME, "body").text:
                break
            time.sleep(1)

        body = driver.find_element(By.TAG_NAME, "body").text
        missing = [label for label, needle in REQUIRED if needle not in body]
        if missing:
            print(
                "refusing to save: the panel does not show " + ", ".join(missing) + ".\n"
                "The README describes this screenshot as showing all three, so a capture\n"
                "without them would make the document assert something the image does not.\n"
                "Check the app has a GEMINI_API_KEY and budget left, then re-run.",
                file=sys.stderr,
            )
            return 1

        # Drop focus first. Streamlit rings the focused input in red, which in a
        # still image reads as a validation error on the question -- the opposite
        # of what the screenshot is evidence for.
        driver.execute_script("document.activeElement && document.activeElement.blur();")
        time.sleep(1)

        # Crop from the heading to the cost caption, in DOCUMENT coordinates.
        #
        # Not by screenshotting a container element: Streamlit's wrappers do not
        # correspond to the visible panel, and walking up N ancestors captured
        # the heading alone -- a 4 KB image of two words, which is precisely the
        # silent-wrong-artifact this script exists to avoid. The two ends of the
        # region are found by their text instead, which is what a reader sees.
        rect = driver.execute_script(
            """
            const all = [...document.querySelectorAll('*')].filter(e => e.children.length === 0);
            const find = t => all.find(e => (e.textContent || '').includes(t));
            const top = find('Ask your own');
            const bottom = find('LIMIT added by the guard');
            if (!top || !bottom) return null;
            const a = top.getBoundingClientRect(), b = bottom.getBoundingClientRect();
            const pad = 20;
            return {
              x: Math.min(a.left, b.left) + scrollX - pad,
              y: a.top + scrollY - pad,
              width: Math.max(a.right, b.right) - Math.min(a.left, b.left) + pad * 2,
              height: (b.bottom + scrollY) - (a.top + scrollY) + pad * 2,
            };
            """
        )
        if not rect or rect["height"] < 100:
            print("could not locate the answer panel to crop", file=sys.stderr)
            return 1

        shot = driver.execute_cdp_cmd(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {**{k: float(v) for k, v in rect.items()}, "scale": 2},
            },
        )

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(base64.b64decode(shot["data"]))
        print(
            f"wrote {args.out}  ({args.out.stat().st_size // 1024} KB, "
            f"{int(rect['width'])}x{int(rect['height'])} css px at 2x)"
        )
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
