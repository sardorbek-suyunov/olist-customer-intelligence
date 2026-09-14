"""
Semantic search over the review embeddings, to see whether they mean anything.

    python scripts/search_reviews.py --neighbours-of 3 --k 5
    python scripts/search_reviews.py --text "o produto chegou quebrado"

"35,616 vectors written" is not a result. An embedding run that silently
produced garbage -- the wrong task type, a merged batch, an un-normalised vector
-- writes exactly the same number of rows as one that worked, and every
downstream feature built on it would look fine until someone read the output.
So the deliverable is neighbours a Portuguese speaker would call sensible.

TWO MODES, BECAUSE THE DEMO NEEDS BOTH
--------------------------------------
--neighbours-of   review-to-review. Both sides are RETRIEVAL_DOCUMENT vectors
                  already in the warehouse, so this costs nothing and tests the
                  stored vectors directly.
--text            a natural-language query, embedded as RETRIEVAL_QUERY. This is
                  the asymmetric pair the app actually uses, and it costs one
                  embed call (fractions of a cent). Worth testing separately:
                  document-document similarity can look fine while query-document
                  is broken, because the task types are different vector spaces.

Similarity is a dot product, not a full cosine: the vectors are unit-normalised
at every supported dimensionality and `enrichment.embed` verifies that on every
batch, so the denominator is 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment.store import RAW_SCHEMA, embedding_table  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def neighbours(con, table: str, vector: list[float], k: int, exclude: str | None = None):
    """
    Top-k by cosine similarity, with the labels alongside.

    The aspects come from the enrichment tables rather than from the embedding,
    so the two independent views of the same review can be compared by eye. If
    the nearest neighbours of a delivery complaint are labelled with product
    aspects, one of the two is wrong and this is where that shows up.
    """
    return con.execute(
        f"""
        with scored as (
            select
                e.content_hash,
                array_cosine_similarity(e.embedding, ?::float[{len(vector)}]) as similarity
            from {RAW_SCHEMA}.{table} e
            where ? is null or e.content_hash <> ?
        )
        select s.similarity, r.aspects, r.sentiment, r.severity, s.content_hash
        from scored s
        left join {RAW_SCHEMA}.review_enrichment r
               on r.content_hash = s.content_hash
        order by s.similarity desc
        limit {k}
        """,
        [vector, exclude, exclude],
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    import duckdb

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--model", default="gemini-embedding-2")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--text", action="append", default=[], help="natural-language query")
    parser.add_argument(
        "--neighbours-of", type=int, default=0, help="how many seed reviews to show neighbours for"
    )
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args(argv)

    from enrichment.embed import DOCUMENT_TASK, embed_query
    from enrichment.enrich import load_reviews
    from enrichment.store import content_hash

    table = embedding_table(args.dimensions)
    variant = f"{DOCUMENT_TASK}:{args.dimensions}"

    texts: dict[str, str] = {}
    for _, text in load_reviews(None, 11):
        texts.setdefault(content_hash(text, variant, args.model), text)

    con = duckdb.connect(str(args.duckdb_path), read_only=True)
    try:
        total = con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
        print(f"{total:,} vectors, {args.dimensions}-d, model {args.model}\n")

        def show(rows) -> None:
            for rank, (similarity, aspects, sentiment, _severity, digest) in enumerate(rows, 1):
                labels = ", ".join(json.loads(aspects)) if aspects else "-"
                body = texts.get(digest, "<text not in corpus>")
                print(f"    {rank}. {similarity:.4f}  [{sentiment or '?'}: {labels}]")
                print(f"       {body[:110]}")

        if args.neighbours_of:
            import random

            rng = random.Random(args.seed)
            # Seeds long enough to be about something. A two-word review has no
            # content for a neighbour to be similar TO, so it would test nothing.
            candidates = [
                h
                for h, t in texts.items()
                if 60 <= len(t) <= 160
                and con.execute(
                    f"select 1 from {RAW_SCHEMA}.{table} where content_hash = ?", [h]
                ).fetchone()
            ]
            for digest in rng.sample(candidates, min(args.neighbours_of, len(candidates))):
                vector = con.execute(
                    f"select embedding from {RAW_SCHEMA}.{table} where content_hash = ?", [digest]
                ).fetchone()[0]
                print(f"  SEED: {texts[digest][:110]}")
                show(neighbours(con, table, list(vector), args.k, exclude=digest))
                print()

        if args.text:
            from enrichment.client import make_client

            client = make_client()
            for query in args.text:
                vector = embed_query(client, args.model, query, args.dimensions)
                print(f"  QUERY: {query!r}  (embedded as RETRIEVAL_QUERY)")
                show(neighbours(con, table, vector, args.k))
                print()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
