"""
Embed the distinct review texts, through the logged path.

    python -m enrichment.embed --all --dimensions 1536
    python -m enrichment.embed --sample 200 --budget-usd 0.02

TWO THINGS THIS API DOES THAT THE LABELLING API DOES NOT
---------------------------------------------------------
1. `contents=["a", "b", "c"]` returns ONE embedding, not three. The SDK folds a
   list of strings into a single Content with three parts and embeds the
   concatenation. It does not error, it does not warn, and the vector it returns
   is a perfectly ordinary unit vector -- it is simply the wrong one, for a text
   nobody wrote. Passing `list[types.Content]` returns one embedding per text.

   Every batch is therefore checked for arity before anything is stored, the
   same guard `parse_batch` applies to a labelling response that silently covers
   19 of 20 reviews. This is that bug in a new place, and it is louder here
   because a merged embedding is indistinguishable from a real one downstream.

2. The response carries NO usage_metadata. There is nothing to record, so the
   cost log cannot store what the API says it charged the way the labelling log
   does. Tokens are counted with `countTokens` instead -- free, and the same
   tokenizer, so it is exact rather than estimated -- and the cost row is marked
   as computed. That is a weaker provenance than usage_metadata and it is
   written down rather than glossed: reconcile against the billing console
   before quoting the number anywhere.

Vectors come back L2-normalised at every supported dimensionality, so cosine
similarity is a dot product and no re-normalisation is needed after truncation.
Verified on every batch rather than assumed, because a silently un-normalised
vector would degrade search quality without failing anything.
"""

from __future__ import annotations

import argparse
import logging
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from enrichment.client import GeminiError, make_client, with_retries
from enrichment.pricing import cost_usd, price_for
from enrichment.store import CostRow, DuckDBStore, content_hash

LOG = logging.getLogger("olist.embed")

ROOT = Path(__file__).resolve().parents[1]

# The task type is part of what the vector MEANS. RETRIEVAL_DOCUMENT and
# RETRIEVAL_QUERY are an asymmetric pair: the stored side is embedded as a
# document, the user's question as a query, and mixing them up degrades search
# quietly rather than loudly. It travels in the cache key for the same reason
# the model and prompt version do.
DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"


class ItemRateLimiter:
    """
    A token bucket over TEXTS, not over calls.

    The quota that stopped two full-corpus runs is
    `EmbedContentPerMinutePerProjectPerUserPerModel-PaidTier`, limit 3,000. The
    obvious reading is 3,000 requests a minute, and this job makes only ~1,400
    of those in total -- so on that reading it could not possibly trip, and it
    tripped twice.

    It counts the CONTENTS: a batch of 50 texts spends 50. That is 35,616 for
    the corpus, so the floor on wall time is about twelve minutes no matter how
    many workers are pointed at it.

    Retrying does not help against a per-minute quota, it just spends attempts
    waiting for a window that opens on a schedule. Pacing does. The default sits
    under the published limit rather than at it, because the bucket and the
    server's window do not share a clock.
    """

    def __init__(self, per_minute: int, burst_fraction: float = 0.1) -> None:
        self.rate = per_minute / 60.0
        # A classic token bucket starts FULL, and that is what kept tripping the
        # quota even at a rate below it: the first moment released a whole
        # minute's allowance at once, and the server's window is evidently
        # sliding rather than a clean minute boundary, so the burst plus the
        # refill behind it crossed the line while the average never did.
        #
        # So the bucket holds a fraction of a minute and starts EMPTY. The
        # average rate is unchanged; what goes away is the ability to spend it
        # all in the first second.
        self.capacity = max(1.0, per_minute * burst_fraction)
        self._tokens = 0.0
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, items: int) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= items:
                    self._tokens -= items
                    return
                deficit = items - self._tokens
            time.sleep(min(5.0, deficit / self.rate))


def embed_batch(
    client, model: str, texts: list[str], dimensions: int, task: str
) -> list[list[float]]:
    """
    One call, one vector per text, checked.

    list[Content] rather than list[str]: see the module docstring. The arity
    assertion is the point of this function existing at all.
    """
    from google.genai import types

    # Through with_retries, the same path GeminiClient.generate uses. The first
    # version of this called the SDK directly and died on a 429 after 3,187
    # vectors: the embed quota is per-minute and eight workers clear it easily.
    response = with_retries(
        lambda: client.models.embed_content(
            model=model,
            contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
            config=types.EmbedContentConfig(output_dimensionality=dimensions, task_type=task),
        ),
        what=f"embed_content({len(texts)} texts)",
    )
    vectors = [list(e.values) for e in response.embeddings]

    if len(vectors) != len(texts):
        raise GeminiError(
            f"asked for {len(texts)} embeddings and got {len(vectors)}. The list[str] "
            "form of `contents` folds the batch into one Content and embeds the "
            "concatenation; use list[types.Content]."
        )
    for index, vector in enumerate(vectors):
        if len(vector) != dimensions:
            raise GeminiError(f"embedding {index} has {len(vector)} dims, expected {dimensions}")
        norm = math.sqrt(sum(x * x for x in vector))
        if abs(norm - 1.0) > 1e-3:
            raise GeminiError(
                f"embedding {index} is not unit-normalised (norm {norm:.4f}). Cosine "
                "similarity downstream assumes it is."
            )
    return vectors


def embed_query(client, model: str, text: str, dimensions: int) -> list[float]:
    """A search query, embedded as a QUERY rather than as a document."""
    return embed_batch(client, model, [text], dimensions, QUERY_TASK)[0]


def count_batch_tokens(client, model: str, texts: list[str]) -> int:
    """
    Exact token total for a batch, in one call instead of len(texts) calls.

    countTokens over a list of Contents returns EXACTLY one token less per
    Content than counting each separately -- measured at 1.00/text for batches
    of 5, 20 and 50. That gap is per-item framing, and since each text is
    embedded on its own, the billed figure is the individual sum. So the batched
    call is corrected by `+ len(texts)` and the result is exact rather than
    approximate.

    Without the correction this undercounts the corpus by about 5%, which is
    small, plausible, and invisible -- the shape of every cost bug this project
    has already found. `verify_token_correction` checks the relationship still
    holds before a run relies on it.
    """
    from google.genai import types

    batched = with_retries(
        lambda: client.models.count_tokens(
            model=model,
            contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
        ),
        what=f"count_tokens({len(texts)} texts)",
    ).total_tokens
    return batched + len(texts)


def verify_token_correction(client, model: str, texts: list[str]) -> None:
    """
    Prove the +1/text correction on real texts before trusting it for 35,616.

    Six free calls. A tokenizer change that altered the per-Content framing would
    otherwise silently shift every cost figure this module writes, and the cost
    log would stay internally consistent while drifting from the bill.
    """
    sample = texts[:5]
    individually = sum(
        client.models.count_tokens(model=model, contents=t).total_tokens for t in sample
    )
    corrected = count_batch_tokens(client, model, sample)
    if corrected != individually:
        raise GeminiError(
            f"batched countTokens + {len(sample)} = {corrected}, but counting the same "
            f"texts individually gives {individually}. The per-Content framing this "
            "module corrects for has changed; re-measure it before trusting any cost row."
        )
    LOG.info("token correction verified on %s texts: %s tokens either way", len(sample), corrected)


def load_texts(sample: int | None, seed: int) -> list[tuple[str, str]]:
    """(review_id, text) for the distinct texts, deduplicated exactly as labelling does."""
    from enrichment.enrich import load_reviews

    rows = load_reviews(sample, seed)
    distinct: dict[str, tuple[str, str]] = {}
    for review_id, text in rows:
        distinct.setdefault(text, (review_id, text))
    return list(distinct.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemini-embedding-2")
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=0.25,
        help="hard ceiling. Refuses to start if the projection exceeds it.",
    )
    parser.add_argument("--workers", type=int, default=8, help="concurrent network calls")
    parser.add_argument(
        "--rate-per-minute",
        type=int,
        default=2500,
        help="texts per minute. The published quota counts CONTENTS, not calls; "
        "the default sits under 3000 rather than at it.",
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
        LOG.error(
            "%s has no price in enrichment/pricing.py. cost_usd() would record 0.00 and "
            "the budget ceiling would never engage. Refusing.",
            args.model,
        )
        return 3

    texts = load_texts(None if args.all else args.sample, args.seed)
    LOG.info("%s distinct texts in scope", f"{len(texts):,}")

    # The cache key is (text, task+dimensions, model) -- a different dimensionality
    # is a different vector, so it must not answer from a cached one.
    variant = f"{DOCUMENT_TASK}:{args.dimensions}"
    hashes = [content_hash(text, variant, args.model) for _, text in texts]

    store = DuckDBStore(str(args.duckdb_path))
    try:
        store.create_embedding_table(args.dimensions)
        cached = store.cached_embedding_hashes([*hashes])
        todo = [(h, t) for h, (_, t) in zip(hashes, texts, strict=True) if h not in cached]
        LOG.info("%s cached, %s to embed", f"{len(cached):,}", f"{len(todo):,}")

        if not todo:
            LOG.info("nothing to do: this run costs $0.00")
            store.write_cost(
                CostRow(
                    run_id=uuid.uuid4().hex[:12],
                    model=args.model,
                    prompt_version=variant,
                    batch_size=args.batch_size,
                    reviews=0,
                    input_tokens=0,
                    output_tokens=0,
                    candidates_tokens=0,
                    thoughts_tokens=0,
                    cost_usd=0.0,
                    wall_seconds=0.0,
                )
            )
            return 0

        client = client or make_client()

        # Pre-flight, priced from the measured rate rather than a guess:
        # scripts/cost_embeddings.py counted 19.29 tokens per review against the
        # real corpus and reconciled it to a real bill.
        projected = cost_usd(args.model, int(len(todo) * 19.29), 0)
        LOG.info(
            "projected $%.4f for %s texts (budget $%.2f)",
            projected,
            f"{len(todo):,}",
            args.budget_usd,
        )
        if projected > args.budget_usd:
            LOG.error(
                "REFUSING TO START: projected $%.4f exceeds --budget-usd $%.2f",
                projected,
                args.budget_usd,
            )
            return 3

        verify_token_correction(client, args.model, [t for _, t in todo])

        run_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        tokens = 0
        embedded = 0

        batches = [todo[i : i + args.batch_size] for i in range(0, len(todo), args.batch_size)]

        limiter = ItemRateLimiter(args.rate_per_minute)

        def work(chunk: list[tuple[str, str]]):
            """Network only. The DuckDB write stays on the main thread."""
            batch_texts = [t for _, t in chunk]
            # Only the embed spends this quota. The metric is named
            # `embed_content_paid_tier_requests` but the evidence says it counts
            # CONTENTS: the first run died at 3,187 vectors -- essentially 3,000
            # contents -- after only ~64 HTTP calls, which is nowhere near 3,000
            # requests. countTokens is a different endpoint with its own limit.
            limiter.acquire(len(batch_texts))
            vectors = embed_batch(client, args.model, batch_texts, args.dimensions, DOCUMENT_TASK)
            return chunk, vectors, count_batch_tokens(client, args.model, batch_texts)

        # Threads because this is entirely network-bound, and serially it is an
        # hour and a half. The connection is not shared with them: results come
        # back to the main thread to be written.
        #
        # The cost row is written in the FINALLY, not after the loop. The first
        # full-corpus attempt died on a 429 with 2,800 vectors already stored and
        # no row recorded at all -- the spend was real, the log said it never
        # happened, and only counting the stored rows afterwards revealed it.
        # A run that crashes must still say what it spent before it crashed.
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for chunk, vectors, batch_tokens in pool.map(work, batches):
                    tokens += batch_tokens
                    store.write_embeddings(
                        [
                            (h, args.model, variant, args.dimensions, v)
                            for (h, _), v in zip(chunk, vectors, strict=True)
                        ]
                    )
                    embedded += len(chunk)
                    if embedded % (args.batch_size * 40) < args.batch_size or embedded == len(todo):
                        LOG.info(
                            "  %s/%s embedded  %s tokens  $%.4f  %.0fs",
                            f"{embedded:,}",
                            f"{len(todo):,}",
                            f"{tokens:,}",
                            cost_usd(args.model, tokens, 0),
                            time.monotonic() - started,
                        )
        finally:
            spent = cost_usd(args.model, tokens, 0)
            store.write_cost(
                CostRow(
                    run_id=run_id,
                    model=args.model,
                    prompt_version=variant,
                    batch_size=args.batch_size,
                    reviews=embedded,
                    input_tokens=tokens,
                    output_tokens=0,
                    candidates_tokens=0,
                    thoughts_tokens=0,
                    cost_usd=spent,
                    wall_seconds=time.monotonic() - started,
                )
            )
            LOG.info(
                "run %s: %s embedded, %s tokens, $%.4f in %.0fs",
                run_id,
                f"{embedded:,}",
                f"{tokens:,}",
                spent,
                time.monotonic() - started,
            )
        LOG.info(
            "tokens are COUNTED (countTokens), not reported by the embed response. "
            "Reconcile against the billing console before quoting this."
        )
    finally:
        store.con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
