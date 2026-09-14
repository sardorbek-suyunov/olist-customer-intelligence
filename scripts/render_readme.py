"""
Render README.md from README.template.md and the measured figures.

The README is the project's credibility surface, and every number on it was
previously typed by hand from docs/data_profiling.md. That drifted -- the seller
city merge said 611 -> 603 while the data said 611 -> 604, the test count said 68
where dbt reported 55, and CI's own comment said 67. Three values, three
different wrong answers, all in a document whose argument is that its numbers are
measured rather than remembered.

Asserting the README against the profiling report would only detect that. This
removes the second copy instead: README.md is generated, so there is nothing for
it to disagree with.

    make readme          # regenerate README.md
    make readme-check    # fail if the committed README is stale (CI)

Figures come from two measured sources and nowhere else:

  docs/figures.json            written by scripts/profile_dataset.py, from the data
  transform/target/manifest.json + pytest --collect-only, from the build itself

A placeholder with no figure behind it is an error, not an empty string. Silently
rendering nothing is how a claim disappears without anyone noticing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "README.template.md"
OUT = ROOT / "README.md"
FIGURES = ROOT / "docs" / "figures.json"
MANIFEST = ROOT / "transform" / "target" / "manifest.json"

PLACEHOLDER = re.compile(r"\{\{([a-z0-9_]+)\}\}")

# Test suites `make test` runs. Counted by asking pytest, not by reading the
# files: parametrised cases expand at collection time and a hand count of `def
# test_` would understate them.
PYTEST_TARGETS = ["analytics/tests", "ingestion/tests", "enrichment/tests"]


def build_figures() -> dict[str, object]:
    """Counts that come from the build rather than from the source data."""
    if not MANIFEST.exists():
        raise SystemExit(
            f"{MANIFEST} not found. The model and test counts come from the dbt\n"
            "manifest, so the project has to be built first:\n"
            "  make build"
        )

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    nodes = manifest["nodes"].values()

    # Models the build actually materialises. The project defines one more --
    # int_customer_address_versions is ephemeral, so it is inlined into its
    # consumers and never appears as a node in `dbt build` output. Counting all
    # 13 here would make the README disagree with the log it describes.
    models_built = sum(
        1
        for n in nodes
        if n["resource_type"] == "model" and n["config"]["materialized"] != "ephemeral"
    )

    return {
        "dbt_models": models_built,
        "dbt_models_defined": sum(1 for n in nodes if n["resource_type"] == "model"),
        "dbt_tests": sum(1 for n in nodes if n["resource_type"] == "test"),
        "python_tests": count_pytest(PYTEST_TARGETS),
        "analytics_tests": count_pytest(["analytics/tests"]),
    }


def count_pytest(targets: list[str]) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *targets],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if not match:
        raise SystemExit(
            f"could not read a test count from pytest for {targets}:\n{result.stdout[-2000:]}"
        )
    return int(match.group(1))


def format_figure(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def render() -> str:
    if not FIGURES.exists():
        raise SystemExit(f"{FIGURES} not found. Run `make profile` first.")

    figures: dict[str, object] = json.loads(FIGURES.read_text(encoding="utf-8"))
    figures.update(build_figures())

    template = TEMPLATE.read_text(encoding="utf-8")
    missing: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in figures:
            missing.add(name)
            return match.group(0)
        return format_figure(figures[name])

    rendered = PLACEHOLDER.sub(substitute, template)

    if missing:
        raise SystemExit(
            "README.template.md references figures that nothing measures: "
            + ", ".join(sorted(missing))
            + "\nAdd them in scripts/profile_dataset.py (data) or build_figures() (build)."
        )

    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed README.md differs from a fresh render.",
    )
    args = parser.parse_args(argv)

    rendered = render()

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != rendered:
            print(
                "README.md is stale: it does not match README.template.md rendered\n"
                "against the current figures. Regenerate it with:\n"
                "  make profile && make readme",
                file=sys.stderr,
            )
            return 1
        print("README.md is current.")
        return 0

    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
