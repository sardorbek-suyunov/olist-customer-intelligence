"""
Load one replay slice into the warehouse.

The second half of the ingestion pair: `replay.py` cuts the static extract into
referentially-consistent Parquet slices, this loads one into raw tables.

    python -m ingestion.load --slice data/slices/purchase_date=2017-03-01 --target duckdb
    python -m ingestion.load --slice data/slices/purchase_date=2017-03-01 --target bigquery
    python -m ingestion.load --slice data/slices/purchase_date=2017-03-01 --verify

IDEMPOTENCY
-----------
An Airflow task gets retried, and a backfill gets re-run. Loading the same
slice twice must leave the warehouse in the same state as loading it once, so
every row carries the slice's `_slice_date` and a load is delete-then-insert
scoped to that value. Append-only would silently double the month.

COMPLETENESS
------------
A slice is five independent load operations with no transaction spanning them.
An interruption between table two and table three leaves the warehouse torn --
and reconciliation arithmetic cannot see it, because the tables that were never
reached contribute no numbers to disagree with.

So completeness is recorded rather than inferred. `slice_load_manifest` gets one
row per slice, written only after every table that slice owes has landed and
been counted from the warehouse. A torn slice is then detectable by absence
instead of by arithmetic, and since the load itself is idempotent the repair is
just a re-run: the interrupted attempt leaves no marker, the retry overwrites
whatever partial state it finds, and the marker lands at the end.

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

# Completeness marker. No leading underscore: BigQuery treats `_`-prefixed
# tables as hidden, which is the wrong property for the one table an operator
# goes looking for when a DAG run ends ambiguously.
MANIFEST_TABLE = "slice_load_manifest"

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


def expected_stems(slice_dir: Path) -> list[str]:
    """
    The tables this slice owes the warehouse.

    Derived from the files replay actually wrote, not from TABLE_MAP: a day in
    the 2018 tail can legitimately have an order with no items, and demanding an
    order_items load there would fail every sparse slice.
    """
    return [stem for stem in TABLE_MAP if (slice_dir / f"{stem}.parquet").exists()]


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------
def load_duckdb(slice_dir: Path, loaded_on: date, db_path: Path) -> dict[str, int]:
    import duckdb

    con = duckdb.connect(str(db_path))
    con.execute(f"create schema if not exists {RAW_SCHEMA}")
    counts: dict[str, int] = {}

    try:
        for stem, table in TABLE_MAP.items():
            parquet = slice_dir / f"{stem}.parquet"
            if not parquet.exists():
                LOG.debug("No %s in slice, skipping", stem)
                continue

            # Logged before the work, not after. When a load hangs or is killed,
            # this line is the only record of which table was in flight.
            LOG.info("loading %s", table)

            fq = f"{RAW_SCHEMA}.{table}"
            # The column already exists in the file (replay writes it), so this is
            # a straight copy -- same bytes the BigQuery loader sends.
            source = f"select * from read_parquet('{parquet.as_posix()}')"

            # CTAS on first sight, then delete-then-insert so a retry is a no-op.
            exists = con.execute(
                "select count(*) from information_schema.tables "
                "where table_schema = ? and table_name = ?",
                [RAW_SCHEMA, table],
            ).fetchone()[0]

            if not exists:
                con.execute(f"create table {fq} as {source}")
            else:
                con.execute(
                    f"delete from {fq} "
                    f"where cast({SLICE_COLUMN} as date) = date '{loaded_on.isoformat()}'"
                )
                con.execute(f"insert into {fq} {source}")

            counts[table] = con.execute(
                f"select count(*) from {fq} "
                f"where cast({SLICE_COLUMN} as date) = date '{loaded_on.isoformat()}'"
            ).fetchone()[0]
    finally:
        con.close()

    return counts


def mark_duckdb(loaded_on: date, counts: dict[str, int], db_path: Path) -> None:
    import duckdb

    con = duckdb.connect(str(db_path))
    try:
        # An empty slice is marked without ever having loaded a table, so this
        # cannot assume load_duckdb already created the schema.
        con.execute(f"create schema if not exists {RAW_SCHEMA}")
        con.execute(
            f"create table if not exists {RAW_SCHEMA}.{MANIFEST_TABLE} ("
            "  slice_date date, loaded_at timestamp, table_count integer, row_counts varchar)"
        )
        con.execute(f"delete from {RAW_SCHEMA}.{MANIFEST_TABLE} where slice_date = ?", [loaded_on])
        con.execute(
            f"insert into {RAW_SCHEMA}.{MANIFEST_TABLE} values (?, current_timestamp, ?, ?)",
            [loaded_on, len(counts), json.dumps(counts, sort_keys=True)],
        )
    finally:
        con.close()


def duckdb_marker(loaded_on: date, db_path: Path) -> dict | None:
    import duckdb

    if not db_path.exists():
        return None

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        present = con.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = ? and table_name = ?",
            [RAW_SCHEMA, MANIFEST_TABLE],
        ).fetchone()[0]
        if not present:
            return None

        row = con.execute(
            f"select table_count, row_counts from {RAW_SCHEMA}.{MANIFEST_TABLE} "
            "where slice_date = ?",
            [loaded_on],
        ).fetchone()
        return {"table_count": row[0], "row_counts": json.loads(row[1])} if row else None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------
def raw_dataset() -> str:
    """
    The raw dataset, guarded against being the mart dataset.

    These must be two real datasets, not two env vars pointing at one. Past the
    "table not found" confusion of conflating them, the dashboard service account
    is to be IAM-scoped to marts alone -- which is not expressible if raw lives in
    the same dataset. Checked on every load, because separate today is not
    separate after someone edits an environment.
    """
    raw = os.environ.get("BQ_RAW_DATASET", "olist_raw")
    marts = os.environ.get("BQ_DATASET", "olist")
    if raw == marts:
        raise SystemExit(
            f"BQ_RAW_DATASET and BQ_DATASET are both {raw!r}. They must be distinct "
            "datasets: raw is loaded by ingestion, marts are written by dbt, and the "
            "dashboard service account is scoped to marts alone."
        )
    return raw


def _bigquery_count(client, project: str, dataset: str, table: str, loaded_on: date) -> int:
    """
    Count what is actually in the partition.

    Not `job.output_rows`: that reports what this job wrote, so a second load of
    the same slice returns the same number whether the partition was replaced or
    doubled -- it cannot distinguish the two states it exists to tell apart.

    Range predicate rather than `date(_slice_date) = @d` so the scan prunes to the
    one partition instead of reading the column across all of them.
    """
    from google.cloud import bigquery

    sql = (
        f"select count(*) as n from `{project}.{dataset}.{table}` "
        f"where {SLICE_COLUMN} >= timestamp(@d) "
        f"and {SLICE_COLUMN} < timestamp_add(timestamp(@d), interval 1 day)"
    )
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", loaded_on)]
    )
    return next(iter(client.query(sql, job_config=config).result())).n


def ensure_dataset(client, project: str, dataset: str) -> None:
    """
    Create the raw dataset if this is a fresh project.

    Called from both the load and the marker paths: an empty slice is marked
    without loading anything, so the marker can be the first thing a project
    ever writes.
    """
    from google.cloud import bigquery

    reference = bigquery.Dataset(f"{project}.{dataset}")
    reference.location = os.environ.get("BQ_LOCATION", "US")
    client.create_dataset(reference, exists_ok=True)


def load_bigquery(slice_dir: Path, loaded_on: date, dataset: str, project: str) -> dict[str, int]:
    """
    Load into date-partitioned raw tables in the RAW dataset.

    Uses WRITE_TRUNCATE against a partition decorator (`table$YYYYMMDD`), which
    replaces exactly that partition atomically -- the BigQuery-native way to
    get the same idempotency the DuckDB path gets from delete-then-insert,
    without a DML statement per retry.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=project)

    # The raw dataset is created by whoever writes first, so a fresh project
    # needs no manual setup step before `make backfill TARGET=bigquery`.
    ensure_dataset(client, project, dataset)

    counts: dict[str, int] = {}

    for stem, table in TABLE_MAP.items():
        parquet = slice_dir / f"{stem}.parquet"
        if not parquet.exists():
            continue

        # Logged before the work, not after. When a load hangs or is killed,
        # this line is the only record of which table was in flight.
        LOG.info("loading %s", table)

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

        counts[table] = _bigquery_count(client, project, dataset, table, loaded_on)
        LOG.debug("Loaded %s rows into %s", counts[table], target)

    return counts


def mark_bigquery(loaded_on: date, counts: dict[str, int], dataset: str, project: str) -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    ensure_dataset(client, project, dataset)
    table = f"{project}.{dataset}.{MANIFEST_TABLE}"

    client.query(
        f"create table if not exists `{table}` ("
        "  slice_date date, loaded_at timestamp, table_count int64, row_counts string)"
    ).result()

    # delete-then-insert, for the reason the data tables use it: a retry must
    # leave one marker, not two.
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("d", "DATE", loaded_on),
            bigquery.ScalarQueryParameter("n", "INT64", len(counts)),
            bigquery.ScalarQueryParameter("c", "STRING", json.dumps(counts, sort_keys=True)),
        ]
    )
    client.query(
        f"delete from `{table}` where slice_date = @d; "
        f"insert into `{table}` values (@d, current_timestamp(), @n, @c);",
        job_config=config,
    ).result()


def bigquery_marker(loaded_on: date, dataset: str, project: str) -> dict | None:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    table = f"{project}.{dataset}.{MANIFEST_TABLE}"
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", loaded_on)]
    )
    try:
        rows = list(
            client.query(
                f"select table_count, row_counts from `{table}` where slice_date = @d",
                job_config=config,
            ).result()
        )
    except Exception as exc:  # an absent marker table is itself a "not loaded" answer
        LOG.debug("marker lookup failed: %s", exc)
        return None

    if not rows:
        return None
    return {"table_count": rows[0].table_count, "row_counts": json.loads(rows[0].row_counts)}


# ---------------------------------------------------------------------------
def reconcile(slice_dir: Path, manifest: dict, counts: dict[str, int]) -> list[str]:
    """
    Every table the slice owes must be present AND correct.

    The presence half is the one that matters. The old check guarded
    `table in counts`, so a table the loader never reached contributed no entry,
    disagreed with nothing, and passed -- which is exactly how an interrupted
    load reported success.
    """
    problems: list[str] = []

    for stem in expected_stems(slice_dir):
        table = TABLE_MAP[stem]
        if table not in counts:
            problems.append(f"{table}: never loaded ({stem}.parquet exists in the slice)")
            continue

        expected = manifest["row_counts"].get(stem)
        if expected is not None and counts[table] != expected:
            problems.append(f"{table}: manifest says {expected:,}, warehouse has {counts[table]:,}")

    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slice", required=True, type=Path, dest="slice_dir")
    parser.add_argument("--target", default="duckdb")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Report whether the slice is completely loaded; load nothing.",
    )
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
    bigquery_target = args.target.startswith("bigquery")

    project = os.environ.get("GCP_PROJECT_ID")
    if bigquery_target and not project:
        LOG.error("GCP_PROJECT_ID must be set for the bigquery target")
        return 2

    if args.verify:
        loaded_on = slice_date_of(slice_dir)
        marker = (
            bigquery_marker(loaded_on, raw_dataset(), project)
            if bigquery_target
            else duckdb_marker(loaded_on, args.duckdb_path)
        )
        if marker is None:
            LOG.error(
                "Slice %s has no completion marker: it was never loaded, or a load was "
                "interrupted partway. Re-run the load; it is idempotent.",
                loaded_on.isoformat(),
            )
            return 1

        LOG.info(
            "Slice %s complete: %s tables, %s",
            loaded_on.isoformat(),
            marker["table_count"],
            ", ".join(f"{t}={n:,}" for t, n in sorted(marker["row_counts"].items())),
        )
        return 0

    if not slice_dir.exists():
        # Expected whenever replay short-circuited past the end of the extract
        # (ADR 0002) -- the DAG's gate should have skipped this task, but a
        # manual run should not fail either.
        LOG.info("No slice at %s; nothing to load.", slice_dir)
        return 0

    manifest = read_manifest(slice_dir)
    loaded_on = slice_date_of(slice_dir)
    dataset = raw_dataset() if bigquery_target else ""

    def mark(counts: dict[str, int]) -> None:
        if bigquery_target:
            mark_bigquery(loaded_on, counts, dataset, project)
        else:
            mark_duckdb(loaded_on, counts, args.duckdb_path)

    if not manifest["row_counts"].get("orders"):
        # Still complete: a day with no orders owes the warehouse no rows, and
        # it has to be distinguishable from a day whose load was interrupted
        # before it wrote any. Both leave the tables untouched; only one of them
        # is finished. Roughly half the 2018 tail is this case.
        LOG.info("Slice %s contains no orders; nothing to load.", slice_dir.name)
        mark({})
        LOG.info("Slice %s marked complete (empty).", loaded_on.isoformat())
        return 0

    if bigquery_target:
        counts = load_bigquery(slice_dir, loaded_on, dataset, project)
    else:
        counts = load_duckdb(slice_dir, loaded_on, args.duckdb_path)

    LOG.info(
        "Loaded slice %s into %s: %s",
        loaded_on.isoformat(),
        args.target,
        ", ".join(f"{t}={n:,}" for t, n in counts.items()),
    )

    # The manifest is the contract: what replay wrote must be what landed.
    problems = reconcile(slice_dir, manifest, counts)
    if problems:
        for problem in problems:
            LOG.error("%s", problem)
        LOG.error("Slice %s NOT marked complete.", loaded_on.isoformat())
        return 1

    # Only now. The marker is the claim that every table landed, so it is the
    # last thing written and never precedes the check that earns it.
    mark(counts)
    LOG.info("Slice %s marked complete (%s tables).", loaded_on.isoformat(), len(counts))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
