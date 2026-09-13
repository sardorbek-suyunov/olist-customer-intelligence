"""
Enrich Olist review text with aspect labels.

    python -m enrichment.enrich --sample 2000 --batch-size 20 --model <id>
    python -m enrichment.enrich --sample 2000 --batch-size 1  --model <id>   # A/B
    python -m enrichment.enrich --all --batch-size 20 --model <id>

Assume this run will be interrupted. The torn backfill was not a freak event: a
long job on a laptop gets a closed terminal or a sleeping machine, and the only
question is whether the next run repairs or duplicates. So results are written
per batch and every batch checks the cache first -- an interruption costs at most
the batch in flight, and resuming is just running the same command again.

A re-run with an unchanged prompt costs exactly $0, and that is demonstrated by
the cost log rather than asserted: the second run records zero calls, zero
tokens, zero dollars.

Reads review text from archive/ rather than from a warehouse. The CSV is the
extract both warehouses were loaded from, so the text is identical and this
avoids a scan to fetch data we already have on disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

from enrichment import taxonomy
from enrichment.client import GeminiClient, GeminiError
from enrichment.pricing import cost_usd, price_for
from enrichment.store import (
    BigQueryStore,
    CostRow,
    DuckDBStore,
    Labelled,
    Quarantined,
    content_hash,
)

LOG = logging.getLogger("olist.enrich")

ROOT = Path(__file__).resolve().parents[1]
REVIEWS_CSV = ROOT / "archive" / "olist_order_reviews_dataset.csv"


def load_reviews(sample: int | None, seed: int) -> list[tuple[str, str]]:
    """(review_id, text) for reviews that carry a comment, deterministically sampled."""
    import pandas as pd

    frame = pd.read_csv(REVIEWS_CSV, usecols=["review_id", "review_comment_message"])
    frame["text"] = frame.review_comment_message.fillna("").str.strip()
    frame = frame[frame.text != ""]

    if sample is not None and sample < len(frame):
        # Seeded, so the pilot is the same 2,000 reviews at batch size 1 and at
        # batch size 20. An A/B over different reviews would compare nothing.
        frame = frame.sample(sample, random_state=seed)

    return list(frame[["review_id", "text"]].itertuples(index=False, name=None))


def build_prompt(batch: list[tuple[str, str]]) -> str:
    numbered = "\n".join(f"{i}. {text}" for i, (_, text) in enumerate(batch, start=1))
    return f"{taxonomy.prompt_preamble()}\n\nReviews:\n{numbered}"


def parse_batch(
    raw: str, batch: list[tuple[str, str]], hashes: list[str], model: str
) -> tuple[list[Labelled], list[Quarantined]]:
    """
    Turn one response into rows, quarantining anything that does not hold up.

    Schema enforcement happens server-side, so most of this should never fire.
    It fires anyway, because "should never happen" is not a control -- and a
    batched call has a specific failure this must catch: a response that silently
    covers 19 of 20 reviews. Without the per-index check the twentieth review
    would vanish with nothing recorded.
    """
    version = taxonomy.PROMPT_VERSION
    labelled: list[Labelled] = []
    quarantined: list[Quarantined] = []

    def reject(index: int, reason: str, payload: str) -> None:
        quarantined.append(Quarantined(hashes[index], version, model, reason, payload[:4000]))

    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("top level is not a list")
    except Exception as exc:  # noqa: BLE001 - the text is the evidence
        for index in range(len(batch)):
            reject(index, f"unparseable_response: {exc}", raw)
        return labelled, quarantined

    seen: dict[int, dict] = {}
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get("i"), int):
            continue
        position = item["i"] - 1
        if not 0 <= position < len(batch):
            quarantined.append(
                Quarantined("", version, model, "index_out_of_range", json.dumps(item)[:4000])
            )
            continue
        if position in seen:
            reject(position, "duplicate_index", json.dumps(item))
            continue
        seen[position] = item

    for position in range(len(batch)):
        item = seen.get(position)
        if item is None:
            # The batching failure mode. Recorded per review, not as one vague
            # "batch incomplete".
            reject(position, "missing_from_response", raw)
            continue

        aspects = item.get("aspects") or []
        unknown = [a for a in aspects if a not in taxonomy.BY_NAME]
        if unknown:
            reject(position, f"unknown_aspect: {unknown}", json.dumps(item))
            continue
        if item.get("sentiment") not in taxonomy.SENTIMENTS:
            reject(position, f"bad_sentiment: {item.get('sentiment')!r}", json.dumps(item))
            continue
        severity = item.get("severity")
        if not isinstance(severity, int) or not 0 <= severity <= 3:
            reject(position, f"bad_severity: {severity!r}", json.dumps(item))
            continue

        labelled.append(
            Labelled(
                content_hash=hashes[position],
                prompt_version=version,
                model=model,
                aspects=sorted(set(aspects)),
                sentiment=item["sentiment"],
                severity=severity,
                no_content=bool(item.get("no_content", False)),
            )
        )

    return labelled, quarantined


def open_store(args) -> DuckDBStore | BigQueryStore:
    if args.target.startswith("bigquery"):
        project = os.environ.get("GCP_PROJECT_ID")
        if not project:
            raise SystemExit("GCP_PROJECT_ID must be set for the bigquery target")
        return BigQueryStore(project, os.environ.get("BQ_RAW_DATASET", "olist_raw"))
    return DuckDBStore(args.duckdb_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="exact model id; see make gemini-check")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--sample", type=int, default=None, help="deterministic subset")
    parser.add_argument("--all", action="store_true", help="every review with text")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--target", default="duckdb")
    parser.add_argument(
        "--duckdb-path",
        type=Path,
        default=Path(os.environ.get("DBT_DUCKDB_PATH", ROOT / "transform" / "olist.duckdb")),
    )
    parser.add_argument(
        "--batch-api",
        action="store_true",
        help="price the run at Batch API rates (half of standard)",
    )
    parser.add_argument(
        "--max-calls", type=int, default=None, help="stop after N API calls; a crude ceiling"
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        help="hard ceiling. Refuses to start if the projection exceeds it, and aborts "
        "mid-run the moment actual spend crosses it.",
    )
    parser.add_argument(
        "--usd-per-1k",
        type=float,
        default=None,
        help="measured cost per 1,000 reviews for the pre-flight projection; "
        "take it from a calibration run rather than guessing.",
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="0 disables thinking. Thinking tokens bill at the output rate and "
        "dominated the first pilot's cost.",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, client=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    )

    if not args.all and args.sample is None:
        LOG.error("pass --sample N or --all")
        return 2

    if price_for(args.model) is None:
        LOG.warning(
            "no price on file for %s; the cost log will record 0.00 rather than a guess",
            args.model,
        )

    reviews = load_reviews(None if args.all else args.sample, args.seed)
    LOG.info("%s reviews with text in scope", f"{len(reviews):,}")

    version = taxonomy.PROMPT_VERSION
    hashes = [content_hash(text, version, args.model) for _, text in reviews]

    store = open_store(args)
    try:
        # The map is written for every review in scope, cached or not: it is what
        # dbt joins on, and a cache hit still needs its review pointed at a result.
        store.write_map(list(zip([r for r, _ in reviews], hashes, strict=True)))

        distinct: dict[str, tuple[str, str]] = {}
        for (review_id, text), digest in zip(reviews, hashes, strict=True):
            distinct.setdefault(digest, (review_id, text))
        LOG.info(
            "%s distinct texts (%s duplicates answered from the key alone)",
            f"{len(distinct):,}",
            f"{len(reviews) - len(distinct):,}",
        )

        cached = store.cached_hashes(list(distinct))
        todo = [(h, *v) for h, v in distinct.items() if h not in cached]
        LOG.info("%s cached, %s to label", f"{len(cached):,}", f"{len(todo):,}")

        if not todo:
            LOG.info("nothing to do: this run costs $0.00")
            store.write_cost(
                CostRow(
                    uuid.uuid4().hex[:12], args.model, version, args.batch_size, 0, 0, 0, 0.0, 0.0
                )
            )
            return 0

        # Pre-flight. maximum_bytes_billed rejects a query before it bills; this
        # refuses a run before it spends, on the same principle -- a ceiling
        # enforced by the tool rather than by whoever is watching.
        if args.budget_usd is not None and args.usd_per_1k:
            projected = len(todo) / 1000 * args.usd_per_1k
            LOG.info(
                "projected %.2f USD for %s reviews at %.3f/1k (budget %.2f)",
                projected,
                f"{len(todo):,}",
                args.usd_per_1k,
                args.budget_usd,
            )
            if projected > args.budget_usd:
                LOG.error(
                    "REFUSING TO START: projected $%.2f exceeds --budget-usd $%.2f. "
                    "Reduce --sample, lower the rate with --thinking-budget 0, or "
                    "raise the budget deliberately.",
                    projected,
                    args.budget_usd,
                )
                return 3
        elif args.budget_usd is not None:
            LOG.warning(
                "--budget-usd given without --usd-per-1k: no pre-flight projection is "
                "possible, but the run still aborts the moment actual spend crosses it"
            )

        if client is None:
            client = GeminiClient(args.model, thinking_budget=args.thinking_budget)

        run_id = uuid.uuid4().hex[:12]
        totals = {"in": 0, "out": 0, "cost": 0.0, "wall": 0.0, "labelled": 0, "bad": 0}
        calls = 0
        started = time.monotonic()

        for offset in range(0, len(todo), args.batch_size):
            if args.max_calls is not None and calls >= args.max_calls:
                LOG.warning("stopping at --max-calls=%s; re-run to resume", args.max_calls)
                break

            chunk = todo[offset : offset + args.batch_size]
            batch = [(rid, text) for _, rid, text in chunk]
            batch_hashes = [h for h, _, _ in chunk]

            try:
                raw, usage = client.generate(build_prompt(batch), taxonomy.response_schema())
            except GeminiError as exc:
                LOG.error("batch at offset %s failed: %s", offset, exc)
                store.write_quarantine(
                    [
                        Quarantined(h, version, args.model, f"call_failed: {exc}", "")
                        for h in batch_hashes
                    ]
                )
                totals["bad"] += len(batch)
                calls += 1
                continue

            labelled, quarantined = parse_batch(raw, batch, batch_hashes, args.model)

            # Written per batch. An interruption here costs this batch and nothing
            # before it.
            store.write_results(labelled)
            store.write_quarantine(quarantined)

            calls += 1
            totals["in"] += usage.input_tokens
            totals["out"] += usage.output_tokens
            totals["wall"] += usage.wall_seconds
            totals["cost"] += cost_usd(
                args.model, usage.input_tokens, usage.output_tokens, args.batch_api
            )
            totals["labelled"] += len(labelled)
            totals["bad"] += len(quarantined)

            # The ceiling that does not depend on a projection being right.
            if args.budget_usd is not None and totals["cost"] >= args.budget_usd:
                LOG.error(
                    "STOPPING: spent $%.4f, at or over --budget-usd $%.2f. "
                    "%s reviews labelled and stored; re-run to resume.",
                    totals["cost"],
                    args.budget_usd,
                    f"{totals['labelled']:,}",
                )
                break

            if calls % 10 == 0 or offset + args.batch_size >= len(todo):
                LOG.info(
                    "  %s/%s calls  labelled=%s quarantined=%s  $%.4f",
                    calls,
                    -(-len(todo) // args.batch_size),
                    f"{totals['labelled']:,}",
                    totals["bad"],
                    totals["cost"],
                )

        store.write_cost(
            CostRow(
                run_id=run_id,
                model=args.model,
                prompt_version=version,
                batch_size=args.batch_size,
                reviews=totals["labelled"],
                input_tokens=totals["in"],
                output_tokens=totals["out"],
                cost_usd=totals["cost"],
                wall_seconds=time.monotonic() - started,
            )
        )

        LOG.info(
            "run %s: %s labelled, %s quarantined, %s in / %s out tokens, $%.4f",
            run_id,
            f"{totals['labelled']:,}",
            totals["bad"],
            f"{totals['in']:,}",
            f"{totals['out']:,}",
            totals["cost"],
        )
        LOG.info("cumulative: %s", store.totals())
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
