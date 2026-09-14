"""
Measure what a demo-shaped VECTOR_SEARCH actually scans, against the ceiling.

    python scripts/measure_vector_search_bytes.py --load
    python scripts/measure_vector_search_bytes.py          # dry runs only

WHY NOT ARITHMETIC
------------------
35,616 x 1536 floats is ~437 MiB, which sits comfortably under the 1 GiB
per-query ceiling in analytics/bq_safety.py. That number is correct and it is
not the number that matters, because the app never runs a bare vector search --
it searches, then joins the hits to orders and to the aspect mart to say
anything useful. Those joins scan columns of their own, and the total is what
the ceiling applies to.

So every query below is dry-run against real BigQuery, which returns the exact
bytes the job would bill for without running it or spending anything. The
queries get progressively more realistic, ending at the shape the demo will
actually issue, and each is compared to DEFAULT_MAX_BYTES_PER_QUERY rather than
to a round number.

A dry run is free. Loading is free. Only storage costs anything, and 437 MiB is
about a cent a month.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.bq_safety import (  # noqa: E402
    DEFAULT_MAX_BYTES_PER_QUERY,
    DEFAULT_MAX_BYTES_PER_SESSION,
    _human,
)
from enrichment.store import RAW_SCHEMA, embedding_table  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def export_parquet(duckdb_path: Path, dimensions: int, out: Path, model: str) -> int:
    """
    DuckDB -> Parquet, with the embedding as a plain list of DOUBLE.

    BigQuery's VECTOR_SEARCH takes ARRAY<FLOAT64>. DuckDB's fixed-size
    ARRAY(FLOAT) does not map to that on its own, so the cast is explicit rather
    than left to whatever the writer guesses.

    THE REVIEW_ID CANNOT BE JOINED FROM THE MAP.
    `review_enrichment_map` is keyed on the LABEL hash -- content_hash(text,
    'v1', flash-lite) -- and an embedding row carries content_hash(text,
    'RETRIEVAL_DOCUMENT:1536', embedding-2). Same review, two different hashes,
    by design: the variant is part of the cache key. Joining them directly
    returns zero rows, which is what the first version of this did. It exported
    an empty Parquet, loaded an empty table, and the measurement then reported
    every query as comfortably inside the ceiling.

    So the bridge is built in Python from the one `content_hash` definition:
    embedding hash -> text -> label hash -> review_id.
    """
    import duckdb

    from enrichment.enrich import load_reviews
    from enrichment.store import content_hash

    variant = f"RETRIEVAL_DOCUMENT:{dimensions}"
    label_variant, label_model = "v1", "gemini-3.1-flash-lite"

    bridge = []
    for review_id, text in load_reviews(None, 11):
        bridge.append(
            (
                content_hash(text, variant, model),
                content_hash(text, label_variant, label_model),
                review_id,
            )
        )

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        table = embedding_table(dimensions)
        rows = con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
        con.execute(
            "create temp table bridge(embed_hash varchar, label_hash varchar, review_id varchar)"
        )
        con.executemany("insert into bridge values (?,?,?)", bridge)
        con.execute(
            f"""copy (
                    select
                        e.content_hash,
                        b.review_id,
                        cast(e.embedding as double[]) as embedding
                    from {RAW_SCHEMA}.{table} e
                    join (
                        select embed_hash, min(review_id) as review_id
                        from bridge group by embed_hash
                    ) b on b.embed_hash = e.content_hash
                ) to '{out.as_posix()}' (format parquet, compression zstd)"""
        )
        exported = con.execute(
            f"""select count(*) from {RAW_SCHEMA}.{table} e
                join (select distinct embed_hash from bridge) b
                  on b.embed_hash = e.content_hash"""
        ).fetchone()[0]
        if exported != rows:
            raise SystemExit(
                f"{rows:,} vectors stored but only {exported:,} could be matched to a "
                "review_id. The corpus and the vectors disagree; do not measure against this."
            )
        return rows
    finally:
        con.close()


QUERIES: list[tuple[str, str]] = [
    (
        "1. bare VECTOR_SEARCH (the number arithmetic gives you)",
        """
        select base.review_id, distance
        from vector_search(
            table `{project}.{dataset}.{table}`, 'embedding',
            (select [{vector}] as embedding), top_k => 10, distance_type => 'COSINE')
        """,
    ),
    (
        "2. + the review text, which the demo must display",
        """
        select base.review_id, distance, r.review_comment_message
        from vector_search(
            table `{project}.{dataset}.{table}`, 'embedding',
            (select [{vector}] as embedding), top_k => 10, distance_type => 'COSINE')
        join `{project}.{raw}.olist_order_reviews_dataset` r
          on r.review_id = base.review_id
        """,
    ),
    (
        "3. + order context (the join the app actually does)",
        """
        select base.review_id, distance, r.review_comment_message,
               o.order_status, o.order_value, o.delivery_days
        from vector_search(
            table `{project}.{dataset}.{table}`, 'embedding',
            (select [{vector}] as embedding), top_k => 10, distance_type => 'COSINE')
        join `{project}.{raw}.olist_order_reviews_dataset` r
          on r.review_id = base.review_id
        join `{project}.{marts}.fct_orders` o
          on o.order_id = r.order_id
        """,
    ),
    (
        "4. FULL DEMO SHAPE: search + text + orders + segment aspects",
        """
        with hits as (
            select base.review_id, base.content_hash, distance
            from vector_search(
                table `{project}.{dataset}.{table}`, 'embedding',
                (select [{vector}] as embedding), top_k => 10, distance_type => 'COSINE')
        )
        select h.review_id, h.distance, r.review_comment_message,
               o.order_status, o.order_value, o.delivery_days,
               s.rfm_segment, s.aspect, s.aspect_rate_of_reviewed,
               s.review_text_coverage_pct
        from hits h
        join `{project}.{raw}.olist_order_reviews_dataset` r
          on r.review_id = h.review_id
        join `{project}.{marts}.fct_orders` o
          on o.order_id = r.order_id
        join `{project}.{marts}.fct_segment_aspect` s
          on s.aspect is not null
        """,
    ),
]


def main(argv: list[str] | None = None) -> int:
    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--dataset", default="olist_marts")
    parser.add_argument("--raw-dataset", default="olist_raw")
    parser.add_argument("--table", default="review_embeddings")
    parser.add_argument("--model", default="gemini-embedding-2")
    parser.add_argument("--load", action="store_true", help="export and upload the vectors first")
    args = parser.parse_args(argv)

    client = bigquery.Client()
    project = client.project
    fq = f"{project}.{args.dataset}.{args.table}"

    if args.load:
        out = ROOT / "data" / f"review_embeddings_{args.dimensions}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = export_parquet(args.duckdb_path, args.dimensions, out, args.model)
        size = out.stat().st_size
        print(f"exported {rows:,} vectors -> {out.name} ({_human(size)})")
        # TWO STEPS, AND BOTH ARE NECESSARY.
        #
        # Loading this Parquet with an INFERRED schema gives BigQuery a RECORD
        # column -- the data is all there, 420 MiB of it, and VECTOR_SEARCH
        # refuses the type. Loading it with an EXPLICIT REPEATED FLOAT64 schema
        # gives the right type and silently DROPS every value: 35,616 rows load,
        # the job succeeds, the table reports 3.4 MiB, and every array is empty.
        #
        # The second failure is much worse than the first, because a dry run
        # against it returns "1.2 MiB, OK". The query only fails when it is
        # actually executed: "Dimension of column embedding in the base table
        # does not match". A dry run validates the shape of the statement, not
        # the content of the table, so a measurement built on dry runs alone
        # reported a comfortable pass on a table that could not be searched.
        #
        # So: land it as RECORD, where the values survive, then flatten to a
        # real ARRAY<FLOAT64> in SQL -- and verify the dimensions afterwards
        # rather than trusting that the load said it worked.
        staging = f"{fq}_staging"
        client.load_table_from_file(
            out.open("rb"),
            staging,
            job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.PARQUET,
                write_disposition="WRITE_TRUNCATE",
            ),
        ).result()
        client.query(
            f"""create or replace table `{fq}` as
                select
                    content_hash,
                    review_id,
                    array(select e.element from unnest(embedding.list) as e) as embedding
                from `{staging}`"""
        ).result()
        client.delete_table(staging, not_found_ok=True)

        dims = list(
            client.query(
                f"select array_length(embedding) as dims, count(*) as n "
                f"from `{fq}` group by dims order by n desc"
            ).result()
        )
        if len(dims) != 1 or dims[0].dims != args.dimensions:
            raise SystemExit(
                "Vectors did not survive the load: "
                + ", ".join(f"{d.n:,} rows at {d.dims} dims" for d in dims)
                + f". Expected every row at {args.dimensions}."
            )
        print(f"  verified: every row is {dims[0].dims}-dimensional")

    table = client.get_table(fq)
    print(f"{fq}: {table.num_rows:,} rows, {_human(table.num_bytes)} stored")
    if table.num_rows == 0:
        # The first run of this printed "Worst case 0.0 B, infx inside the
        # per-query ceiling" against an empty table -- a pass describing a
        # measurement that never happened, which is the exact failure this
        # repository keeps cataloguing, committed by the script written to
        # measure one. Refuse rather than report.
        raise SystemExit(
            "The embedding table is EMPTY. Nothing was measured; a ceiling check "
            "against no data is not a result. Load it with --load first."
        )
    print(
        f"ceiling: {_human(DEFAULT_MAX_BYTES_PER_QUERY)}/query, "
        f"{_human(DEFAULT_MAX_BYTES_PER_SESSION)}/session\n"
    )

    # A literal vector, so the dry run prices the real statement rather than a
    # parameterised stand-in. Zeros scan exactly what any other vector would.
    row = next(iter(client.query(f"select embedding from `{fq}` limit 1").result()))
    vector = ",".join(repr(float(x)) for x in row.embedding)
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)

    worst = 0
    measured: list[dict] = []
    for label, template in QUERIES:
        sql = template.format(
            project=project,
            dataset=args.dataset,
            raw=args.raw_dataset,
            marts=args.dataset,
            table=args.table,
            vector=vector,
        )
        try:
            estimated = client.query(sql, job_config=config).total_bytes_processed
            # EXECUTED, not only planned. A dry run has now approved two queries
            # in this script that could not run: one against a table whose arrays
            # had silently loaded empty, and one whose probe vector was all
            # zeros. Both times it returned a comfortable number. The billed
            # figure is the one that describes what happened.
            job = client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    use_query_cache=False,
                    maximum_bytes_billed=DEFAULT_MAX_BYTES_PER_QUERY,
                ),
            )
            returned = len(list(job.result()))
            scanned = job.total_bytes_billed
        except Exception as exc:  # noqa: BLE001 - the message is the finding
            print(f"  {label}\n      FAILED: {str(exc)[:160]}\n")
            measured.append({"query": label, "failed": str(exc)[:200]})
            continue
        worst = max(worst, scanned)
        pct = 100 * scanned / DEFAULT_MAX_BYTES_PER_QUERY
        verdict = "OK" if scanned <= DEFAULT_MAX_BYTES_PER_QUERY else "EXCEEDS THE CEILING"
        print(f"  {label}")
        print(
            f"      dry {_human(estimated):>10}  BILLED {_human(scanned):>10}  "
            f"{pct:5.1f}% of ceiling  {verdict}  ({returned} rows)"
        )
        measured.append(
            {
                "query": label,
                "dry_run_bytes": int(estimated),
                "billed_bytes": int(scanned),
                "pct_of_query_ceiling": round(pct, 1),
                "rows": returned,
            }
        )

    print()
    if worst > DEFAULT_MAX_BYTES_PER_QUERY:
        print("The demo-shaped query does NOT fit. Narrow the search table or drop dimensions.")
        return 1
    if worst == 0:
        raise SystemExit("Every query failed to plan; there is nothing to compare.")
    headroom = DEFAULT_MAX_BYTES_PER_QUERY / worst
    print(f"Worst case {_human(worst)}, {headroom:.1f}x inside the per-query ceiling.")
    per_session = DEFAULT_MAX_BYTES_PER_SESSION // worst
    print(f"Session ceiling allows {per_session} such queries -- the TIGHTER constraint.")

    out_json = ROOT / "enrichment" / "eval" / "vector_search_bytes.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(
            {
                "dimensions": args.dimensions,
                "rows": table.num_rows,
                "query_ceiling_bytes": DEFAULT_MAX_BYTES_PER_QUERY,
                "session_ceiling_bytes": DEFAULT_MAX_BYTES_PER_SESSION,
                "worst_billed_bytes": int(worst),
                "worst_pct_of_query_ceiling": round(100 * worst / DEFAULT_MAX_BYTES_PER_QUERY, 1),
                "headroom_x": round(headroom, 1),
                "searches_per_session": int(per_session),
                "queries": measured,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out_json.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
