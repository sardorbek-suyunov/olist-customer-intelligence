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


def export_parquet(duckdb_path: Path, dimensions: int, out: Path) -> int:
    """
    DuckDB -> Parquet, with the embedding as a plain list of DOUBLE.

    BigQuery's VECTOR_SEARCH takes ARRAY<FLOAT64>. DuckDB's fixed-size
    ARRAY(FLOAT) does not map to that on its own, so the cast is explicit here
    rather than left to whatever the writer guesses.
    """
    import duckdb

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        table = embedding_table(dimensions)
        rows = con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
        con.execute(
            f"""copy (
                    select
                        e.content_hash,
                        m.review_id,
                        cast(e.embedding as double[]) as embedding
                    from {RAW_SCHEMA}.{table} e
                    join (
                        select content_hash, min(review_id) as review_id
                        from {RAW_SCHEMA}.review_enrichment_map
                        group by content_hash
                    ) m on m.content_hash = e.content_hash
                ) to '{out.as_posix()}' (format parquet, compression zstd)"""
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
    parser.add_argument("--load", action="store_true", help="export and upload the vectors first")
    args = parser.parse_args(argv)

    client = bigquery.Client()
    project = client.project
    fq = f"{project}.{args.dataset}.{args.table}"

    if args.load:
        out = ROOT / "data" / f"review_embeddings_{args.dimensions}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = export_parquet(args.duckdb_path, args.dimensions, out)
        size = out.stat().st_size
        print(f"exported {rows:,} vectors -> {out.name} ({_human(size)})")
        job = client.load_table_from_file(
            out.open("rb"),
            fq,
            job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.PARQUET,
                write_disposition="WRITE_TRUNCATE",
            ),
        )
        job.result()
        table = client.get_table(fq)
        print(f"loaded {table.num_rows:,} rows, {_human(table.num_bytes)} in BigQuery\n")

    table = client.get_table(fq)
    print(f"{fq}: {table.num_rows:,} rows, {_human(table.num_bytes)} stored")
    print(
        f"ceiling: {_human(DEFAULT_MAX_BYTES_PER_QUERY)}/query, "
        f"{_human(DEFAULT_MAX_BYTES_PER_SESSION)}/session\n"
    )

    # A literal vector, so the dry run prices the real statement rather than a
    # parameterised stand-in. Zeros scan exactly what any other vector would.
    vector = ",".join(["0.0"] * args.dimensions)
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)

    worst = 0
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
            job = client.query(sql, job_config=config)
        except Exception as exc:  # noqa: BLE001 - the message is the finding
            print(f"  {label}\n      FAILED: {str(exc)[:160]}\n")
            continue
        scanned = job.total_bytes_processed
        worst = max(worst, scanned)
        pct = 100 * scanned / DEFAULT_MAX_BYTES_PER_QUERY
        verdict = "OK" if scanned <= DEFAULT_MAX_BYTES_PER_QUERY else "EXCEEDS THE CEILING"
        print(f"  {label}")
        print(f"      {_human(scanned):>10}   {pct:5.1f}% of the per-query ceiling   {verdict}")

    print()
    if worst > DEFAULT_MAX_BYTES_PER_QUERY:
        print("The demo-shaped query does NOT fit. Narrow the search table or drop dimensions.")
        return 1
    headroom = DEFAULT_MAX_BYTES_PER_QUERY / worst if worst else float("inf")
    print(f"Worst case {_human(worst)}, {headroom:.1f}x inside the per-query ceiling.")
    print(f"Session ceiling allows {DEFAULT_MAX_BYTES_PER_SESSION // worst} such queries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
