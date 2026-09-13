"""
Loader tests that run without the Olist dataset present.

Every other test here needs `make data` first, because it builds its slice by
replaying the raw CSVs. That makes the whole ingestion suite conditional on a
123 MB download, which is the wrong shape for the tests that only care about how
a slice is loaded rather than how it was produced.

fixtures/purchase_date=2016-09-01 is one committed slice -- the first month of
the extract, 45 KB, and the only generated Parquet in the repository. It was kept
for two reasons beyond size: every table is populated, and payments (3) do not
match orders (4), so it exercises the child tables not being 1:1 with orders
rather than a case where every count happens to be equal.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from ingestion.load import (
    RAW_SCHEMA,
    TABLE_MAP,
    duckdb_marker,
    main,
    read_manifest,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "purchase_date=2016-09-01"
SLICE_DATE = date(2016, 9, 1)


def count(db: Path, table: str) -> int:
    con = duckdb.connect(str(db))
    try:
        return con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
    finally:
        con.close()


def test_fixture_slice_is_intact() -> None:
    """The fixture is committed data, so a test has to notice if it rots."""
    manifest = read_manifest(FIXTURE)
    assert manifest["logical_date"] == SLICE_DATE.isoformat()
    assert manifest["row_counts"] == {
        "orders": 4,
        "order_items": 6,
        "order_payments": 3,
        "order_reviews": 4,
        "customers": 4,
    }
    for stem in TABLE_MAP:
        assert (FIXTURE / f"{stem}.parquet").exists(), stem


def test_fixture_loads_without_the_source_dataset(tmp_path) -> None:
    db = tmp_path / "fixture.duckdb"
    assert main(["--slice", str(FIXTURE), "--duckdb-path", str(db)]) == 0

    manifest = read_manifest(FIXTURE)
    for stem, table in TABLE_MAP.items():
        assert count(db, table) == manifest["row_counts"][stem], table


def test_fixture_load_is_idempotent(tmp_path) -> None:
    db = tmp_path / "fixture.duckdb"
    assert main(["--slice", str(FIXTURE), "--duckdb-path", str(db)]) == 0
    before = {t: count(db, t) for t in TABLE_MAP.values()}

    assert main(["--slice", str(FIXTURE), "--duckdb-path", str(db)]) == 0

    assert {t: count(db, t) for t in TABLE_MAP.values()} == before


def test_fixture_load_earns_a_completion_marker(tmp_path) -> None:
    db = tmp_path / "fixture.duckdb"
    assert main(["--slice", str(FIXTURE), "--duckdb-path", str(db), "--verify"]) == 1

    assert main(["--slice", str(FIXTURE), "--duckdb-path", str(db)]) == 0

    assert main(["--slice", str(FIXTURE), "--duckdb-path", str(db), "--verify"]) == 0
    marker = duckdb_marker(SLICE_DATE, db)
    assert marker is not None
    assert marker["table_count"] == len(TABLE_MAP)
