"""
Generate the dbt aspect seed from enrichment/taxonomy.py.

    python scripts/export_taxonomy_seed.py
    python scripts/export_taxonomy_seed.py --check    # CI: fail if stale

taxonomy.py's own docstring promises four consumers of one definition: the
prompt, the response schema, the eval harness, and "the dbt seed that validates
the mart". This is that seed, and it is generated rather than maintained for the
same reason the README is -- a hand-written aspect list in SQL is a second copy,
and every bug this project has found was a second copy drifting from the first.

The seed gives the mart a complete grid. Without it a segment that never mentions
`split_shipment` produces no row at all, and "no row" reads as missing data when
the true answer is zero. Those are different claims and the mart should not make
the reader guess which one it is looking at.

`is_delivery_complaint` is derived here from DELIVERY_COMPLAINT_ASPECTS rather
than written as a CASE expression in SQL. That set is the definition; SQL gets a
rendering of it.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment import taxonomy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "transform" / "seeds" / "aspect_taxonomy.csv"

HEADER = ["aspect", "aspect_group", "parent_aspect", "is_delivery_complaint"]


def render() -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for aspect in taxonomy.ASPECTS:
        writer.writerow(
            [
                aspect.name,
                aspect.group,
                aspect.parent or "",
                "true" if aspect.name in taxonomy.DELIVERY_COMPLAINT_ASPECTS else "false",
            ]
        )
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the seed is stale")
    args = parser.parse_args(argv)

    rendered = render()

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != rendered:
            print(
                f"{OUT.relative_to(ROOT)} is stale: it does not match enrichment/taxonomy.py.\n"
                "Regenerate it with:\n  make taxonomy-seed",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT.relative_to(ROOT)} is current.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(taxonomy.ASPECTS)} aspects)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
