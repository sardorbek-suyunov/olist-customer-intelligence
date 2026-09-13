"""
Export the enrichment tables to committed Parquet.

    python scripts/export_enrichment.py --duckdb-path <store>.duckdb

Why these get committed when data/slices does not: replay output is regenerable
from the extract in minutes for free, and enrichment output is not. Reproducing
it costs $3.19 and an API key, and a fork with neither should still be able to
build the aspect marts, run the eval and see the dashboard. That is the same
argument as the dashboard snapshot -- an artifact whose job is to survive the
absence of a credential -- rather than an exception to the rule against
committing derived data.

It is also the cache. `enrichment.enrich` keys on
sha256(text + prompt_version + model), so a clone that loads this snapshot and
re-runs the same command labels nothing and spends nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment.store import COST_LOG, MAP, QUARANTINE, RAW_SCHEMA, RESULTS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "enrichment" / "data"

TABLES = (RESULTS, MAP, QUARANTINE, COST_LOG)


def restore(db_path: Path, source: Path) -> int:
    """
    Load the snapshot into a warehouse, which is what makes a clone free.

    Goes through DuckDBStore rather than CREATE TABLE AS SELECT. CTAS was the
    first attempt and it produced tables with the right rows and no constraints,
    so the next enrichment run died on `insert or replace` with "no UNIQUE/PRIMARY
    KEY constraints that refer to this table". The restored warehouse looked
    perfect and was unusable -- which is why the round trip is tested rather than
    assumed.

    Reusing the store's DDL also means the schema exists in one place.
    """
    import duckdb

    from enrichment.store import DuckDBStore

    store = DuckDBStore(db_path)  # creates every table with its constraints
    try:
        for table in TABLES:
            parquet = source / f"{table}.parquet"
            if not parquet.exists():
                print(f"  skip  {table} (no snapshot)")
                continue
            store.con.execute(f"delete from {RAW_SCHEMA}.{table}")
            store.con.execute(
                f"insert into {RAW_SCHEMA}.{table} "
                f"select * from read_parquet('{parquet.as_posix()}')"
            )
            rows = store.con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
            print(f"  ok    {table:<28} {rows:>8,} rows restored")
    finally:
        store.close()

    # The restore is only useful if the warehouse it produces still works.
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            f"insert or replace into {RAW_SCHEMA}.{MAP} "
            f"select review_id, content_hash from {RAW_SCHEMA}.{MAP} limit 1"
        )
        print("  ok    constraints survived the round trip")
    finally:
        con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    import duckdb

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument(
        "--restore",
        action="store_true",
        help="load the committed Parquet INTO --duckdb-path instead of exporting",
    )
    args = parser.parse_args(argv)

    if args.restore:
        return restore(args.duckdb_path, args.out)

    args.out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(args.duckdb_path), read_only=True)
    total = 0
    try:
        for table in TABLES:
            present = con.execute(
                "select count(*) from information_schema.tables "
                "where table_schema = ? and table_name = ?",
                [RAW_SCHEMA, table],
            ).fetchone()[0]
            if not present:
                print(f"  skip  {table} (absent)")
                continue

            destination = args.out / f"{table}.parquet"
            con.execute(
                f"copy (select * from {RAW_SCHEMA}.{table}) to '{destination.as_posix()}' "
                "(format parquet, compression zstd)"
            )
            rows = con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
            size = destination.stat().st_size
            total += size
            print(f"  ok    {table:<28} {rows:>8,} rows  {size / 1024:>8,.1f} KiB")
    finally:
        con.close()

    print(f"\n  {total / 1024 / 1024:.2f} MiB -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
