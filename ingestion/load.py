"""
Load one replay slice into the warehouse.

The second half of the ingestion pair: `replay.py` cuts the static extract into
referentially-consistent Parquet slices, this loads one into raw tables.

    python -m ingestion.load --slice data/slices/purchase_date=2017-03-01 --target duckdb
    python -m ingestion.load --slice data/slices/purchase_date=2017-03-01 --target bigquery

IDEMPOTENCY
-----------
An Airflow task gets retried, and a backfill gets re-run. Loading the same
slice twice must leave the warehouse in the same state as loading it once, so
every row carries the slice's `_slice_date` and a load is delete-then-insert
scoped to that value. Append-only would silently double the month.

Deliberately no Airflow import: the DAG shells out to this, and this runs by
hand in a terminal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from pathlib import Path

LOG = logging.getLogger("olist.load")

ROOT = Path(__file__).resolve().parents[1]
RAW_SCHEMA = "olist_raw"

# Slice file -> raw table. Matches the names dbt's sources.yml expects.
TABLE_MAP = {
    "orders": "olist_orders_dataset",
    "customers": "olist_customers_dataset",
    "order_items": "olist_order_items_dataset",
    "order_payments": "olist_order_payments_dataset",
    "order_reviews": "olist_order_reviews_dataset",
}

SLICE_COLUMN = "_slice_date"


def read_manifest(slice_dir: Path) -> dict:
    manifest = slice_dir / "_manifest.json"
    if not manifest.exists():
        raise FileNotFoundError(
            f"{manifest} not found. Generate the slice first:\n"
            f"  python -m ingestion.replay --start <date> --end <date>"
        )
    return json.loads(manifest.read_text(encoding="utf-8"))


def slice_date_of(slice_dir: Path) -> date:
    """Parse the Hive partition name, e.g. purchase_date=2017-03-01."""
    name = slice_dir.name
    if "=" not in name:
        raise ValueError(f"Expected a 'purchase_date=YYYY-MM-DD' directory, got {name!r}")
    return date.fromisoformat(name.split("=", 1)[1])


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------
def load_duckdb(slice_dir: Path, loaded_on: date, db_path: Path) -> dict[str, int]:
    import duckdb

    con = duckdb.connect(str(db_path))
    con.execute(f"create schema if not exists {RAW_SCHEMA}")
    counts: dict[str, int] = {}

    for stem, table in TABLE_MAP.items():
        parquet = slice_dir / f"{stem}.parquet"
        if not parquet.exists():
            LOG.debug("No %s in slice, skipping", stem)
            continue

        fq = f"{RAW_SCHEMA}.{table}"
        source = (
            f"select *, date '{loaded_on.isoformat()}' as {SLICE_COLUMN} "
            f"from read_parquet('{parquet.as_posix()}')"
        )

        # CTAS on first sight, then delete-then-insert so a retry is a no-op.
        exists = con.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = ? and table_name = ?",
            [RAW_SCHEMA, table],
        ).fetchone()[0]

        if not exists:
            con.execute(f"create table {fq} as {source}")
        else:
            con.execute(f"delete from {fq} where {SLICE_COLUMN} = date '{loaded_on.isoformat()}'")
            con.execute(f"insert into {fq} {source}")

        counts[table] = con.execute(
            f"select count(*) from {fq} where {SLICE_COLUMN} = date '{loaded_on.isoformat()}'"
        ).fetchone()[0]

    con.close()
    return counts


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------
def load_bigquery(slice_dir: Path, loaded_on: date, dataset: str, project: str) -> dict[str, int]:
    """
    Load into date-partitioned raw tables in the RAW dataset.

    Reads BQ_RAW_DATASET, not BQ_DATASET. The two are different datasets and
    conflating them is a silent failure: raw tables would land in the dataset
    dbt writes models to, while sources.yml keeps looking in the raw one, and
    the build fails with a confusing "table not found".

    Uses WRITE_TRUNCATE against a partition decorator (`table$YYYYMMDD`), which
    replaces exactly that partition atomically -- the BigQuery-native way to
    get the same idempotency the DuckDB path gets from delete-then-insert,
    without a DML statement per retry.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    counts: dict[str, int] = {}

    for stem, table in TABLE_MAP.items():
        parquet = slice_dir / f"{stem}.parquet"
        if not parquet.exists():
            continue

        partition = loaded_on.strftime("%Y%m%d")
        target = f"{project}.{dataset}.{table}${partition}"

        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            time_partitioning=bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field=SLICE_COLUMN
            ),
            autodetect=True,
        )

        with parquet.open("rb") as handle:
            job = client.load_table_from_file(handle, target, job_config=job_config)
        job.result()

        counts[table] = job.output_rows or 0
        LOG.debug("Loaded %s rows into %s", counts[table], target)

    return counts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slice", required=True, type=Path, dest="slice_dir")
    parser.add_argument("--target", default="duckdb")
    parser.add_argument(
        "--duckdb-path",
        type=Path,
        default=Path(os.environ.get("DBT_DUCKDB_PATH", ROOT / "transform" / "olist.duckdb")),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    )

    slice_dir: Path = args.slice_dir
    if not slice_dir.exists():
        # Expected whenever replay short-circuited past the end of the extract
        # (ADR 0002) -- the DAG's gate should have skipped this task, but a
        # manual run should not fail either.
        LOG.info("No slice at %s; nothing to load.", slice_dir)
        return 0

    manifest = read_manifest(slice_dir)
    loaded_on = slice_date_of(slice_dir)

    if not manifest["row_counts"].get("orders"):
        LOG.info("Slice %s contains no orders; nothing to load.", slice_dir.name)
        return 0

    if args.target.startswith("bigquery"):
        project = os.environ.get("GCP_PROJECT_ID")
        if not project:
            LOG.error("GCP_PROJECT_ID must be set for the bigquery target")
            return 2
        counts = load_bigquery(
            slice_dir, loaded_on, os.environ.get("BQ_RAW_DATASET", "olist_raw"), project
        )
    else:
        counts = load_duckdb(slice_dir, loaded_on, args.duckdb_path)

    LOG.info(
        "Loaded slice %s into %s: %s",
        loaded_on.isoformat(),
        args.target,
        ", ".join(f"{t}={n:,}" for t, n in counts.items()),
    )

    # The manifest is the contract: what replay wrote must be what landed.
    for stem, table in TABLE_MAP.items():
        expected = manifest["row_counts"].get(stem)
        if expected is not None and table in counts and counts[table] != expected:
            LOG.error(
                "Row count mismatch for %s: manifest says %s, warehouse has %s",
                table,
                expected,
                counts[table],
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
