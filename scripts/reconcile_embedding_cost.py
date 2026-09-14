"""
Check the cost log against the vectors that actually exist.

    python scripts/reconcile_embedding_cost.py --duckdb-path transform/olist.duckdb
    python scripts/reconcile_embedding_cost.py --write-correction

Two independent facts should agree: how many tokens the cost log says an embed
run consumed, and how many tokens the texts behind the stored vectors actually
contain. When they do not, the log is wrong -- and it is always wrong in the
same direction, because the ways spend escapes a log (a crash before the row is
written, a call made outside the pipeline, a probe) all lose rows rather than
inventing them.

This is the control that would have caught both bypasses in this project:
$0.0863 of exploratory labelling calls made straight through the client, and a
full-corpus embed run that died on a 429 with 2,800 vectors stored and no cost
row at all. Neither was visible from inside the log. Both are obvious the moment
the log is compared against the thing it describes.

The comparison is exact rather than approximate: `countTokens` is free and is
the same tokenizer the billing uses, so the expected figure is a measurement,
not a model. `--write-correction` records the gap as its own cost row, labelled,
rather than quietly adjusting an existing one.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment.client import make_client  # noqa: E402
from enrichment.embed import count_batch_tokens  # noqa: E402
from enrichment.enrich import load_reviews  # noqa: E402
from enrichment.pricing import cost_usd  # noqa: E402
from enrichment.store import CostRow, DuckDBStore, content_hash, embedding_table  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument("--model", default="gemini-embedding-2")
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--write-correction",
        action="store_true",
        help="record the shortfall as its own cost row rather than only reporting it",
    )
    args = parser.parse_args(argv)

    variant = f"RETRIEVAL_DOCUMENT:{args.dimensions}"
    table = embedding_table(args.dimensions)

    store = DuckDBStore(str(args.duckdb_path))
    try:
        stored = {
            h
            for (h,) in store.con.execute(f"select content_hash from olist_raw.{table}").fetchall()
        }
        logged_tokens, logged_usd, rows = store.con.execute(
            """select coalesce(sum(input_tokens), 0), coalesce(sum(cost_usd), 0), count(*)
               from olist_raw.enrichment_cost_log
               where model = ? and prompt_version = ?""",
            [args.model, variant],
        ).fetchone()

        # Recover the texts behind the stored hashes, by hashing the corpus the
        # same way the embed run did. A hash that is stored but unreachable this
        # way would mean the corpus moved under the vectors, which is its own bug.
        texts = {}
        for _, text in load_reviews(None, 11):
            texts.setdefault(content_hash(text, variant, args.model), text)
        reachable = [texts[h] for h in stored if h in texts]
        orphaned = len(stored) - len(reachable)

        client = make_client()
        expected = 0
        for i in range(0, len(reachable), args.batch_size):
            expected += count_batch_tokens(client, args.model, reachable[i : i + args.batch_size])

        expected_usd = cost_usd(args.model, expected, 0)
        gap_tokens = expected - int(logged_tokens)
        gap_usd = expected_usd - float(logged_usd)

        print(f"model            {args.model} @ {variant}")
        print(
            f"vectors stored   {len(stored):,}" + (f"  ({orphaned:,} orphaned)" if orphaned else "")
        )
        print(f"cost rows        {rows}")
        print(f"tokens logged    {int(logged_tokens):,}   (${float(logged_usd):.4f})")
        print(f"tokens expected  {expected:,}   (${expected_usd:.4f})   <- counted from the texts")
        print(f"gap              {gap_tokens:+,}   (${gap_usd:+.4f})")

        if gap_tokens == 0:
            print("\nThe log accounts for every stored vector.")
            return 0

        if gap_tokens < 0:
            print(
                "\nThe log records MORE than the stored vectors justify. That is the "
                "harmless direction -- a re-run that overwrote existing rows, or a "
                "batch counted then discarded -- but it is still a discrepancy."
            )
            return 0

        print(
            f"\nUNLOGGED SPEND: {gap_tokens:,} tokens (${gap_usd:.4f}) were consumed by "
            "vectors that exist with no cost row behind them."
        )
        if not args.write_correction:
            print("Re-run with --write-correction to record it.")
            return 1

        store.write_cost(
            CostRow(
                run_id="reconcile-" + uuid.uuid4().hex[:6],
                model=args.model,
                prompt_version=variant,
                batch_size=args.batch_size,
                reviews=0,
                input_tokens=gap_tokens,
                output_tokens=0,
                candidates_tokens=0,
                thoughts_tokens=0,
                cost_usd=gap_usd,
                wall_seconds=0.0,
            )
        )
        print(f"wrote a correction row for {gap_tokens:,} tokens (${gap_usd:.4f})")
        return 0
    finally:
        store.con.close()


if __name__ == "__main__":
    raise SystemExit(main())
