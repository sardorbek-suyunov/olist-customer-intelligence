"""
Score the NL->SQL agent by EXECUTION accuracy, and explain every failure.

    python scripts/eval_nl2sql.py --validate-gold --backend duckdb
    python scripts/eval_nl2sql.py --backend bigquery --out enrichment/eval/nl2sql.json

Execution accuracy: run the agent's SQL and the gold SQL, compare the RESULT
SETS. Not string similarity, which would reward SQL that looks like mine over
SQL that answers the question -- `count(*)` and `sum(1)` are the same answer and
share almost no characters.

WHAT COUNTS AS A MATCH
----------------------
Values, not column names. The agent calling a column `complaint_rate` where the
gold says `rate` is not a defect, so the comparison is over tuples of values.

Row order is only significant when the gold statement has an ORDER BY. A
question that does not ask for an ordering has no wrong ordering, and comparing
as sequences there would fail correct answers.

Floats are compared to a tolerance. SUM over a different join order produces a
different last bit, and an eval that fails on that is measuring IEEE 754.

THE NUMBER IS REPORTED WITH ITS FAILURES
----------------------------------------
19 of 25 with the six explained is worth more than a claimed 25 of 25. Each
failure is classified by what actually went wrong -- refused by the guard, would
not execute, or executed and returned something else -- because those have
different fixes and lumping them together hides which one to work on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics import settings  # noqa: E402
from analytics.bq_safety import DEFAULT_MAX_BYTES_PER_QUERY  # noqa: E402
from analytics.gold_questions import GOLD  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

FLOAT_TOLERANCE = 1e-6


def normalise(rows: list[tuple], ordered: bool) -> list[tuple]:
    """Round floats, stringify the rest, and sort unless order is significant."""

    def cell(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int,)):
            return float(value)
        if isinstance(value, float):
            return round(value, 6)
        if value is None:
            return None
        return str(value)

    out = [tuple(cell(v) for v in row) for row in rows]
    return out if ordered else sorted(out, key=lambda r: tuple(str(v) for v in r))


def equal(gold_rows: list[tuple], got_rows: list[tuple], ordered: bool) -> bool:
    a, b = normalise(gold_rows, ordered), normalise(got_rows, ordered)
    if len(a) != len(b):
        return False
    for row_a, row_b in zip(a, b, strict=True):
        if len(row_a) != len(row_b):
            return False
        for x, y in zip(row_a, row_b, strict=True):
            if isinstance(x, float) and isinstance(y, float):
                if abs(x - y) > FLOAT_TOLERANCE:
                    return False
            elif x != y:
                return False
    return True


class DuckBackend:
    """Free and local. Used to validate the gold set before spending anything."""

    schema = "main_marts"

    def __init__(self, path: Path) -> None:
        import duckdb

        self.con = duckdb.connect(str(path), read_only=True)

    def run(self, sql: str) -> list[tuple]:
        return self.con.execute(sql).fetchall()

    def close(self) -> None:
        self.con.close()


class BigQueryBackend:
    """What the demo actually runs against."""

    schema = "olist_marts"

    def __init__(self) -> None:
        from google.cloud import bigquery

        self.client = bigquery.Client()

    def run(self, sql: str) -> list[tuple]:
        return [tuple(row.values()) for row in self.client.query(sql).result()]

    def close(self) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["duckdb", "bigquery"], default="bigquery")
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("--validate-gold", action="store_true", help="run the gold SQL only")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    backend = DuckBackend(args.duckdb_path) if args.backend == "duckdb" else BigQueryBackend()
    schema = backend.schema

    try:
        if args.validate_gold:
            print(f"Validating {len(GOLD)} gold statements against {args.backend}\n")
            bad = 0
            for gold in GOLD:
                sql = gold.sql.format(m=schema)
                try:
                    rows = backend.run(sql)
                except Exception as exc:  # noqa: BLE001 - the message is the finding
                    bad += 1
                    print(f"  BROKEN  {gold.id}  {str(exc)[:100]}")
                    continue
                flag = "  EMPTY" if not rows else ""
                if not rows:
                    bad += 1
                print(f"  ok      {gold.id}  {len(rows):>4} rows  {gold.category:9s}{flag}")
            print(f"\n{len(GOLD) - bad}/{len(GOLD)} gold statements return rows.")
            return 1 if bad else 0

        from analytics.nl2sql import Agent

        if args.backend == "duckdb":
            raise SystemExit(
                "The agent targets BigQuery. Use --backend bigquery to score it, or "
                "--validate-gold to check the answer key locally for free."
            )

        # The demo's per-session ceiling is 10 questions, sized for a visitor.
        # An eval is not a visitor, so it gets a session budget sized for the
        # gold set -- but it still draws on the SHARED day and lifetime ledger,
        # because this is real spend against the same key and quietly exempting
        # the eval from the total is how a budget stops meaning anything.
        from analytics.gemini_budget import DemoBudget

        budget = DemoBudget(
            model=args.model,
            **{
                **settings.gemini_budget_settings(),
                "max_calls_per_session": len(GOLD) + 5,
                "max_usd_per_session": 0.05,
            },
        )
        # And the same for the BYTE budget, for the same reason. The session
        # query cap is now derived from what the byte budget affords of the most
        # expensive query shape, which is 11 -- so a 25-question eval run under
        # visitor ceilings dies at question 12 with "Session query limit reached".
        # That is the reconciled ceiling working exactly as intended; an eval is
        # simply not a visitor.
        #
        # The per-QUERY ceiling is left untouched at 1 GiB. That one is not about
        # session length, it is about a single runaway query, and an eval has no
        # claim to a larger one.
        from analytics.bq_safety import QueryBudget

        query_budget = QueryBudget(
            **{
                **settings.query_budget_settings(),
                "max_queries_per_session": len(GOLD) + 5,
                "max_bytes_per_session": (len(GOLD) + 5) * DEFAULT_MAX_BYTES_PER_QUERY,
            }
        )
        agent = Agent(
            backend.client,
            model=args.model,
            demo_budget=budget,
            query_budget=query_budget,
        )
        results = []
        started = time.monotonic()

        for gold in GOLD:
            gold_sql = gold.sql.format(m=schema)
            record = {
                "id": gold.id,
                "question": gold.question,
                "category": gold.category,
                "gold_sql": " ".join(gold_sql.split()),
                "note": gold.note,
                "ordered": gold.ordered,
            }
            try:
                answer = agent.ask(gold.question)
            except Exception as exc:  # noqa: BLE001 - classified below
                record |= {
                    "outcome": "refused" if "Unsafe" in type(exc).__name__ else "error",
                    "detail": str(exc)[:300],
                    "agent_sql": None,
                }
                results.append(record)
                print(f"  FAIL  {gold.id}  {record['outcome']}: {str(exc)[:80]}")
                continue

            record["agent_sql"] = " ".join(answer.sql.split())
            record["usd"] = answer.usd_spent
            record["bytes"] = answer.bytes_billed
            try:
                gold_rows = backend.run(gold_sql)
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(f"gold {gold.id} does not execute: {exc}") from exc

            got = [tuple(r.values()) for r in answer.rows]
            # The QUESTION decides, not the presence of ORDER BY in the gold.
            # Sniffing the SQL failed three correct answers whose gold was
            # ordered only so its output would be stable.
            ordered = gold.ordered
            if equal(gold_rows, got, ordered):
                record["outcome"] = "match"
                print(f"  ok    {gold.id}  {gold.category}")
            else:
                record |= {
                    "outcome": "mismatch",
                    "gold_rows": len(gold_rows),
                    "agent_rows": len(got),
                    "gold_sample": [list(map(str, r)) for r in gold_rows[:3]],
                    "agent_sample": [list(map(str, r)) for r in got[:3]],
                }
                print(
                    f"  FAIL  {gold.id}  mismatch: gold {len(gold_rows)} rows, "
                    f"agent {len(got)} rows"
                )
            results.append(record)

        matched = sum(1 for r in results if r["outcome"] == "match")
        total = len(results)
        by_outcome = Counter(r["outcome"] for r in results)
        by_category = Counter(r["category"] for r in results if r["outcome"] == "match")
        category_totals = Counter(r["category"] for r in results)

        print(f"\nEXECUTION ACCURACY  {matched}/{total}  ({matched / total:.1%})")
        print(
            f"wall {time.monotonic() - started:.0f}s, "
            f"${sum(r.get('usd', 0) for r in results):.5f} of Gemini\n"
        )

        print("  by outcome")
        for outcome, n in by_outcome.most_common():
            print(f"    {outcome:10s} {n}")
        print("\n  by category")
        for category, n in sorted(category_totals.items()):
            print(f"    {category:10s} {by_category.get(category, 0)}/{n}")

        failures = [r for r in results if r["outcome"] != "match"]
        if failures:
            print(f"\n  THE {len(failures)} FAILURES")
            for record in failures:
                print(f"\n    {record['id']} [{record['category']}] {record['question']}")
                if record.get("note"):
                    print(f"      trap: {record['note']}")
                if record["outcome"] == "mismatch":
                    print(f"      gold : {record['gold_sql'][:150]}")
                    print(f"      agent: {record['agent_sql'][:150]}")
                    print(
                        f"      gold {record['gold_rows']} rows vs agent "
                        f"{record['agent_rows']} rows"
                    )
                else:
                    print(f"      {record['outcome']}: {record.get('detail', '')[:160]}")

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(
                    {
                        "model": args.model,
                        "backend": args.backend,
                        "total": total,
                        "matched": matched,
                        "accuracy": round(matched / total, 4),
                        "by_outcome": dict(by_outcome),
                        "by_category": {
                            c: [by_category.get(c, 0), n]
                            for c, n in sorted(category_totals.items())
                        },
                        "results": results,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nwrote {args.out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
