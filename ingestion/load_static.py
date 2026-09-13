"""
Load the tables that are not time-sliced.

products, sellers, geolocation and the category translation are static
reference data with no timestamp to slice on -- the replay path cannot carry
them, but dbt's sources expect all nine tables to exist. Without this step
`dbt build --target bigquery` fails on four missing sources.

    python -m ingestion.load_static --target bigquery
    python -m ingestion.load_static --target duckdb

Idempotent: a full replace each time, which is the right semantic for
reference data small enough to reload in seconds.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

LOG = logging.getLogger("olist.load_static")

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "archive"
RAW_SCHEMA = "olist_raw"

STATIC_TABLES = [
    "olist_products_dataset",
    "olist_sellers_dataset",
    "olist_geolocation_dataset",
    "product_category_name_translation",
]


def load_duckdb(db_path: Path) -> dict[str, int]:
    import duckdb

    con = duckdb.connect(str(db_path))
    con.execute(f"create schema if not exists {RAW_SCHEMA}")
    counts: dict[str, int] = {}
    for table in STATIC_TABLES:
        csv_path = (RAW_DIR / f"{table}.csv").as_posix()
        con.execute(
            f"create or replace table {RAW_SCHEMA}.{table} as "
            f"select * from read_csv_auto('{csv_path}', header=true, sample_size=-1)"
        )
        counts[table] = con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
    con.close()
    return counts


def load_bigquery(dataset: str, project: str) -> dict[str, int]:
    """
    Load via a DataFrame, so column names come from the file rather than from
    BigQuery's guess about it.

    This was CSV + autodetect + skip_leading_rows=1, and it silently mangled
    `product_category_name_translation` into `string_field_0` / `string_field_1`.
    BigQuery's CSV autodetect infers a header by finding a first row whose types
    differ from the rest -- which works for `olist_products_dataset`, where the
    body has numeric columns, and cannot work for a table that is strings all the
    way down. It skipped the header as instructed and then had no names to use.

    `olist_products_dataset` loading correctly is what made this hard to see: the
    same code produced right answers for three tables and a wrong one for the
    fourth, and nothing failed until stg_products joined against it.

    The translation CSV also carries a UTF-8 BOM, which would have prefixed the
    first column name with U+FEFF even had detection worked. utf-8-sig consumes
    it; DuckDB's read_csv_auto already did.

    Parquet is what the slice path sends, so both loaders now hand BigQuery a
    file that carries its own schema and neither depends on inference.
    """
    import pandas as pd
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    dataset_ref = bigquery.Dataset(f"{project}.{dataset}")
    dataset_ref.location = os.environ.get("BQ_LOCATION", "US")
    client.create_dataset(dataset_ref, exists_ok=True)

    counts: dict[str, int] = {}
    for table in STATIC_TABLES:
        frame = pd.read_csv(RAW_DIR / f"{table}.csv", encoding="utf-8-sig")
        job_config = bigquery.LoadJobConfig(
            # Full replace: reference data, and a partial reload would be worse
            # than a clean one.
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        job = client.load_table_from_dataframe(
            frame, f"{project}.{dataset}.{table}", job_config=job_config
        )
        job.result()

        # From the table, not job.output_rows: the latter reports what this job
        # wrote, which cannot tell a replace from a double-write.
        counts[table] = client.get_table(f"{project}.{dataset}.{table}").num_rows
        LOG.info("loaded %s (%s rows)", table, f"{counts[table]:,}")

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="duckdb")
    parser.add_argument(
        "--duckdb-path",
        type=Path,
        default=Path(os.environ.get("DBT_DUCKDB_PATH", ROOT / "transform" / "olist.duckdb")),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    )

    if args.target.startswith("bigquery"):
        project = os.environ.get("GCP_PROJECT_ID")
        if not project:
            LOG.error("GCP_PROJECT_ID must be set for the bigquery target")
            return 2
        counts = load_bigquery(os.environ.get("BQ_RAW_DATASET", "olist_raw"), project)
    else:
        counts = load_duckdb(args.duckdb_path)

    LOG.info(
        "Loaded static tables into %s: %s",
        args.target,
        ", ".join(f"{t}={n:,}" for t, n in counts.items()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
