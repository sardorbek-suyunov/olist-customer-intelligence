"""Tests for the slice loader. DuckDB only -- the BigQuery path needs a project."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from ingestion.load import RAW_SCHEMA, main, raw_dataset, slice_date_of
from ingestion.replay import RAW, slice_window, write_slice

pytestmark = pytest.mark.skipif(
    not (RAW / "olist_orders_dataset.csv").exists(),
    reason="raw dataset not present; run `make data`",
)

WINDOW = (date(2017, 3, 1), date(2017, 4, 1))


@pytest.fixture
def loaded(tmp_path):
    """One slice written and loaded into a throwaway DuckDB."""
    slice_dir = write_slice(slice_window(*WINDOW), tmp_path / "slices", WINDOW[0])
    db = tmp_path / "test.duckdb"
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db)]) == 0
    return slice_dir, db


def count(db, table, slice_date=None) -> int:
    con = duckdb.connect(str(db), read_only=True)
    try:
        sql = f"select count(*) from {RAW_SCHEMA}.{table}"
        if slice_date:
            sql += f" where _slice_date = date '{slice_date}'"
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_slice_lands_with_expected_row_count(loaded) -> None:
    _, db = loaded
    assert count(db, "olist_orders_dataset") == len(slice_window(*WINDOW)["orders"])


def test_reloading_the_same_slice_is_idempotent(loaded) -> None:
    """
    The property an Airflow retry depends on. Append-only would silently
    double the month and every downstream aggregate with it.
    """
    slice_dir, db = loaded
    before = count(db, "olist_orders_dataset")

    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db)]) == 0

    assert count(db, "olist_orders_dataset") == before


def test_slices_coexist_without_clobbering_each_other(loaded, tmp_path) -> None:
    slice_dir, db = loaded
    second = date(2017, 4, 1), date(2017, 5, 1)
    other = write_slice(slice_window(*second), tmp_path / "slices", second[0])
    assert main(["--slice", str(other), "--duckdb-path", str(db)]) == 0

    assert count(db, "olist_orders_dataset", WINDOW[0]) > 0
    assert count(db, "olist_orders_dataset", second[0]) > 0


def test_every_row_is_tagged_with_its_slice_date(loaded) -> None:
    _, db = loaded
    assert count(db, "olist_orders_dataset", WINDOW[0]) == count(db, "olist_orders_dataset")


def test_missing_slice_directory_is_not_an_error(tmp_path) -> None:
    """ADR 0002: past the end of the extract replay writes nothing, and the
    loader must tolerate that rather than fail the DAG."""
    assert main(["--slice", str(tmp_path / "purchase_date=2030-01-01")]) == 0


def test_slice_date_parsed_from_partition_name(tmp_path) -> None:
    assert slice_date_of(tmp_path / "purchase_date=2017-03-01") == date(2017, 3, 1)
    with pytest.raises(ValueError):
        slice_date_of(tmp_path / "not-a-partition")


def test_raw_and_mart_datasets_must_be_distinct(monkeypatch) -> None:
    """
    Two env vars pointing at one dataset is not a split.

    Conflating them is a silent failure -- raw tables land where dbt writes
    models while sources.yml keeps looking elsewhere -- and it also makes the
    dashboard service account impossible to scope to marts alone.
    """
    monkeypatch.setenv("BQ_RAW_DATASET", "olist")
    monkeypatch.setenv("BQ_DATASET", "olist")
    with pytest.raises(SystemExit, match="must be distinct"):
        raw_dataset()


def test_raw_dataset_defaults_are_already_distinct(monkeypatch) -> None:
    monkeypatch.delenv("BQ_RAW_DATASET", raising=False)
    monkeypatch.delenv("BQ_DATASET", raising=False)
    assert raw_dataset() == "olist_raw"
