"""
Execute the demo's example questions and commit their answers.

    python scripts/export_demo_examples.py --duckdb-path transform/olist.duckdb

The public demo must be useful when it cannot spend anything -- because the
budget tripped, because the key lapsed, or because nobody wants a portfolio link
whose first impression depends on an API being up. So the answers are computed
here, at build time, and served as static JSON.

Committed for the same reason as dashboard/data/*.parquet: the demo has to
survive a lapsed credential. A visitor who never asks their own question makes
no API call and no warehouse scan.

The results carry the SQL that produced them. Showing the query alongside the
answer is most of what makes a SQL demo worth looking at, and it means the
cached path demonstrates the same thing the live path would.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.demo_examples import EXAMPLES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dashboard" / "data" / "demo_examples.json"


def main(argv: list[str] | None = None) -> int:
    import duckdb

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument("--marts-schema", default="main_marts")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    con = duckdb.connect(str(args.duckdb_path), read_only=True)
    answers = []
    try:
        for example in EXAMPLES:
            sql = example.sql.format(marts=args.marts_schema).strip()
            frame = con.execute(sql).df()
            answers.append(
                {
                    "id": example.id,
                    "question": example.question,
                    "note": example.note,
                    "sql": sql,
                    "columns": list(frame.columns),
                    # orient="records" keeps the JSON readable and lets the app
                    # render it without knowing the shape in advance.
                    "rows": json.loads(frame.to_json(orient="records")),
                    "row_count": len(frame),
                }
            )
            print(f"  ok    {example.id:32s} {len(frame):>4} rows")
    finally:
        con.close()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "built at release time; the demo serves these with zero API and zero scan",
        "examples": answers,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out.relative_to(ROOT)} ({len(answers)} examples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
