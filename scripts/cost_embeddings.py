"""
Measure what embedding the review corpus would cost, before spending it.

    python scripts/cost_embeddings.py --sample 800

Writes enrichment/eval/embedding_cost.json, which docs/figures.json reads, so the
README quotes a measured number rather than a remembered one.

HOW THE TOKEN COUNT IS REACHED
------------------------------
Not chars/3.5. That rule of thumb sized the workload at planning time and is
superseded by measurement, which is the standing pattern here -- enrichment's
client records `usage_metadata` for the same reason.

`countTokens` is free, so a random sample of texts is counted exactly, a line is
fitted against their character counts, and that line is applied to the exact
character count of all 35,616 distinct texts. Fitting against chars rather than
averaging shrinks the interval by an order of magnitude, because token count and
character count are near-collinear within one language and script.

The estimate was cross-checked once against money actually spent, and that check
is recorded here rather than re-run. At the time, `taxonomy.PROMPT_VERSION` was
v1 and its preamble measured 467 tokens; 467 x 1,781 calls plus 686,882 text
tokens plus 3.44/review of `"N. "` numbering reconciled to the 1,641,229 input
tokens the v1 run was billed for. That is what makes the figure below an
estimate anchored to a real bill rather than a ratio.

It cannot be re-derived now: the v2 prompt lengthened the `product_quality`
description, so `prompt_preamble()` renders 491 tokens and no longer describes
the run that was billed. The preamble recorded below is therefore stamped with
the version it belongs to, and is not the one the reconciliation used.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment import taxonomy  # noqa: E402
from enrichment.client import make_client  # noqa: E402
from enrichment.enrich import load_reviews  # noqa: E402
from enrichment.pricing import cost_usd, price_for  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "enrichment" / "eval" / "embedding_cost.json"

GIB = 1024**3
MIB = 1024**2
# BigQuery stores an embedding as ARRAY<FLOAT64>: 8 bytes per element.
BYTES_PER_DIMENSION = 8
VECTOR_INDEX_MIN_TABLE_BYTES = 10_000_000  # below this, an index is not populated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemini-embedding-2")
    parser.add_argument("--sample", type=int, default=800)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    price = price_for(args.model)
    if price is None:
        raise SystemExit(
            f"{args.model} has no price in enrichment/pricing.py, so this would report "
            "$0.00 rather than a cost. Add the published rate, or pick a priced model."
        )

    texts = sorted({t for _, t in load_reviews(None, 0)})
    total_chars = sum(len(t) for t in texts)
    sample = random.Random(args.seed).sample(texts, min(args.sample, len(texts)))

    client = make_client()

    def count(text: str) -> int:
        for attempt in range(5):
            try:
                return client.models.count_tokens(model=args.model, contents=text).total_tokens
            except Exception as exc:  # noqa: BLE001 - retried, then surfaced
                if "429" not in str(exc) and "RESOURCE_EXHAUSTED" not in str(exc):
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise SystemExit("countTokens rate-limited out; lower --workers")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tokens = list(pool.map(count, sample))
    elapsed = time.monotonic() - started

    chars = [len(t) for t in sample]
    n = len(sample)
    mean_chars = sum(chars) / n
    mean_tokens = sum(tokens) / n
    slope = sum((c - mean_chars) * (t - mean_tokens) for c, t in zip(chars, tokens, strict=True))
    slope /= sum((c - mean_chars) ** 2 for c in chars)
    intercept = mean_tokens - slope * mean_chars
    residual_sd = statistics.stdev(
        [t - (intercept + slope * c) for c, t in zip(chars, tokens, strict=True)]
    )

    estimate = intercept * len(texts) + slope * total_chars
    interval = 1.96 * residual_sd * (len(texts) ** 0.5)

    # The CURRENT preamble, stamped with the version it belongs to. See the
    # module docstring: the reconciliation used v1's 467 tokens, and this is no
    # longer that number.
    preamble = client.models.count_tokens(
        model="gemini-3.1-flash-lite", contents=taxonomy.prompt_preamble()
    ).total_tokens

    result = {
        "model": args.model,
        "distinct_texts": len(texts),
        "total_chars": total_chars,
        "sample_size": n,
        "sample_seconds": round(elapsed, 1),
        "max_tokens_in_sample": max(tokens),
        "fit_intercept": round(intercept, 4),
        "fit_slope": round(slope, 6),
        "tokens": int(round(estimate)),
        "tokens_ci95": int(round(interval)),
        "tokens_per_review": round(estimate / len(texts), 2),
        "preamble_tokens": preamble,
        "preamble_prompt_version": taxonomy.PROMPT_VERSION,
        "usd_standard": round(cost_usd(args.model, int(estimate), 0), 4),
        "usd_batch": round(cost_usd(args.model, int(estimate), 0, batch=True), 4),
        "dimensions": {},
    }

    for dims in (3072, 1536, 768):
        table_bytes = len(texts) * dims * BYTES_PER_DIMENSION
        result["dimensions"][str(dims)] = {
            "table_bytes": table_bytes,
            "table_mib": round(table_bytes / MIB, 1),
            "pct_of_1gib_query_ceiling": round(100 * table_bytes / GIB, 1),
            "times_above_index_floor": round(table_bytes / VECTOR_INDEX_MIN_TABLE_BYTES, 1),
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"{args.model}: {result['tokens']:,} tokens (+/-{result['tokens_ci95']:,}, 95%)")
    print(f"  ${result['usd_standard']} standard, ${result['usd_batch']} batch")
    print(f"  preamble {preamble} tokens; longest sampled text {max(tokens)} tokens")
    for dims, row in result["dimensions"].items():
        print(
            f"  {dims:>5}-d: {row['table_mib']:>7.1f} MiB  "
            f"{row['pct_of_1gib_query_ceiling']:>5.1f}% of the 1 GiB query ceiling  "
            f"{row['times_above_index_floor']:>5.1f}x the 10 MB index floor"
        )
    print(f"\nwrote {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
