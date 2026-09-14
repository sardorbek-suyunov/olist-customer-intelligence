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
    Top-k by cosine similarity. Labels are attached by the caller.

    The label join cannot happen in SQL here. content_hash encodes the VARIANT
    as well as the text -- `RETRIEVAL_DOCUMENT:1536` for an embedding, `v1` for
    a label -- so the same review has two different hashes and joining them
    directly returns nothing. Correct behaviour from the cache key, and a silent
    empty join if you forget it: the first version of this printed every
    neighbour with `[?: -]` where the aspects should have been, and the
    similarity scores looked perfectly fine.

    So the caller maps embedding-hash -> text -> label-hash, both derived from
    the one `content_hash` definition rather than from a second rule.
    """
    return con.execute(
        f"""
        select
            array_cosine_similarity(e.embedding, ?::float[{len(vector)}]) as similarity,
            e.content_hash
        from {RAW_SCHEMA}.{table} e
        where ? is null or e.content_hash <> ?
        order by similarity desc
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
    parser.add_argument(
        "--label-version",
        default=None,
        help="prompt version the labels were produced under; defaults to the shipped run",
    )
    parser.add_argument("--label-model", default="gemini-3.1-flash-lite")
    args = parser.parse_args(argv)

    from enrichment.embed import DOCUMENT_TASK, embed_query
    from enrichment.enrich import load_reviews
    from enrichment.store import content_hash

    table = embedding_table(args.dimensions)
    variant = f"{DOCUMENT_TASK}:{args.dimensions}"
    # The shipped label run, read from dbt_project.yml so this cannot describe
    # a different run from the one the marts were built on.
    import yaml

    project = yaml.safe_load((ROOT / "transform" / "dbt_project.yml").read_text(encoding="utf-8"))
    taxonomy_variant = args.label_version or project["vars"]["shipped_prompt_version"]
    LABEL_MODEL = args.label_model or project["vars"]["shipped_label_model"]

    texts: dict[str, str] = {}
    for _, text in load_reviews(None, 11):
        texts.setdefault(content_hash(text, variant, args.model), text)

    con = duckdb.connect(str(args.duckdb_path), read_only=True)
    try:
        total = con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
        print(f"{total:,} vectors, {args.dimensions}-d, model {args.model}\n")

        # embedding-hash -> label row, bridged through the text. Both hashes come
        # from the same content_hash() definition; only the variant differs.
        label_variant = taxonomy_variant
        labels_by_hash: dict[str, tuple] = {}
        for digest, body in texts.items():
            row = con.execute(
                f"select aspects, sentiment from {RAW_SCHEMA}.review_enrichment "
                "where content_hash = ?",
                [content_hash(body, label_variant, LABEL_MODEL)],
            ).fetchone()
            if row:
                labels_by_hash[digest] = row

        def show(rows) -> None:
            for rank, (similarity, digest) in enumerate(rows, 1):
                row = labels_by_hash.get(digest)
                labels = ", ".join(json.loads(row[0])) if row and row[0] else "-"
                sentiment = row[1] if row else "?"
                body = texts.get(digest, "<text not in corpus>")
                print(f"    {rank}. {similarity:.4f}  [{sentiment}: {labels}]")
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
