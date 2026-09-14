"""
Copy a built mart from DuckDB into BigQuery, via Parquet.

    python scripts/load_mart_to_bq.py fct_segment_aspect

NOT THE PIPELINE. `dbt build --target bigquery` is how these tables are supposed
to get there, and this is a fallback for when that is unavailable -- at the time
of writing dbt-bigquery fails on this machine with a bare "Database Error:
Request couldn't be served." on any command, while the BigQuery client library
queries the same project happily. The adapter is the thing that is broken, not
the credentials and not the warehouse.

The distinction matters for what this script may be used for. It copies a table
that dbt already built and tested on DuckDB, so the CONTENT has been through the
same models and the same 79 tests. What it does not reproduce is BigQuery-side
partitioning, clustering, or the dbt tests running against BigQuery's own
dialect -- so a table loaded this way is fine to query and measure against, and
is not evidence that the BigQuery build works.

Marked here rather than left implicit, because a table that exists in BigQuery
looks identical whether dbt put it there or this did.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", help="mart table name, e.g. fct_segment_aspect")
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    parser.add_argument("--duckdb-schema", default="main_marts")
    parser.add_argument("--bq-dataset", default="olist_marts")
    args = parser.parse_args(argv)

    import duckdb

    scratch = ROOT / "data" / f"_bq_load_{args.table}.parquet"
    scratch.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(args.duckdb_path), read_only=True)
    try:
        rows = con.execute(f"select count(*) from {args.duckdb_schema}.{args.table}").fetchone()[0]
        con.execute(
            f"copy (select * from {args.duckdb_schema}.{args.table}) "
            f"to '{scratch.as_posix()}' (format parquet)"
        )
    finally:
        con.close()

    client = bigquery.Client()
    fq = f"{client.project}.{args.bq_dataset}.{args.table}"
    job = client.load_table_from_file(
        scratch.open("rb"),
        fq,
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition="WRITE_TRUNCATE",
        ),
    )
    job.result()
    table = client.get_table(fq)
    scratch.unlink(missing_ok=True)

    print(f"{args.duckdb_schema}.{args.table}: {rows:,} rows -> {fq}")
    print(f"  {table.num_rows:,} rows, {table.num_bytes / 1024**2:.1f} MiB in BigQuery")
    if table.num_rows != rows:
        print("  WARNING: row counts disagree")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
