"""
Completeness of a slice load, including a deliberately torn one.

The loader writes five tables with no transaction spanning them, so an
interruption partway leaves the warehouse holding part of a slice. That state is
not hypothetical -- it is what a real backfill left behind on 2018-10-01, with
orders and customers loaded and order_payments and order_reviews missing, and
nothing reporting a problem.

A detector nobody has watched fire is an assumption, so the test here does not
mock an exception: it starts the loader as a subprocess, waits for the third
table to be announced, and kills the process. No cleanup handler runs, exactly
as with a `kill -9`, a closed terminal, or a machine going to sleep.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import pytest

from ingestion.load import (
    MANIFEST_TABLE,
    RAW_SCHEMA,
    TABLE_MAP,
    duckdb_marker,
    main,
    reconcile,
)
from ingestion.replay import RAW, slice_window, write_slice

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (RAW / "olist_orders_dataset.csv").exists(),
    reason="raw dataset not present; run `make data`",
)

WINDOW = (date(2017, 3, 1), date(2017, 4, 1))

# Kill once this many tables have been announced. Three is the first index that
# proves the point: two tables are committed, the rest are not, and the marker
# cannot have been written because it is written after all five.
TEAR_AFTER = 3


@pytest.fixture
def slice_dir(tmp_path) -> Path:
    return write_slice(slice_window(*WINDOW), tmp_path / "slices", WINDOW[0])


def table_count(db: Path, table: str) -> int:
    con = duckdb.connect(str(db))
    try:
        present = con.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = ? and table_name = ?",
            [RAW_SCHEMA, table],
        ).fetchone()[0]
        return (
            con.execute(f"select count(*) from {RAW_SCHEMA}.{table}").fetchone()[0]
            if present
            else 0
        )
    finally:
        con.close()


def tear_the_load(slice_dir: Path, db: Path) -> list[str]:
    """Run the loader for real and kill it as the third table begins."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "ingestion.load",
            "--slice",
            str(slice_dir),
            "--duckdb-path",
            str(db),
            "--log-level",
            "INFO",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    announced: list[str] = []
    try:
        for line in proc.stderr:  # type: ignore[union-attr]
            if "loading " not in line:
                continue
            announced.append(line.rsplit("loading ", 1)[1].strip())
            if len(announced) == TEAR_AFTER:
                proc.kill()
                break
    finally:
        proc.wait(timeout=30)
        if proc.stderr:
            proc.stderr.close()

    return announced


def test_interrupted_load_is_detected_and_repairs_on_retry(slice_dir, tmp_path) -> None:
    db = tmp_path / "torn.duckdb"

    announced = tear_the_load(slice_dir, db)
    assert announced == list(TABLE_MAP.values())[:TEAR_AFTER], (
        f"the loader did not get far enough to tear: announced {announced}"
    )

    # Torn: the first two tables are committed, the last two were never reached.
    landed = [t for t in TABLE_MAP.values() if table_count(db, t) > 0]
    assert TABLE_MAP["orders"] in landed
    assert TABLE_MAP["order_reviews"] not in landed

    # The detector fires. This is the assertion the whole test exists for: the
    # old reconciliation passed on exactly this state.
    assert duckdb_marker(WINDOW[0], db) is None
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db), "--verify"]) == 1

    # Self-healing: the retry overwrites the partial state and earns the marker.
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db)]) == 0
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db), "--verify"]) == 0

    marker = duckdb_marker(WINDOW[0], db)
    assert marker is not None
    assert marker["table_count"] == len(TABLE_MAP)
    assert marker["row_counts"][TABLE_MAP["orders"]] == len(slice_window(*WINDOW)["orders"])


def test_repaired_slice_is_not_doubled(slice_dir, tmp_path) -> None:
    """The repair must converge, not append -- a torn load followed by a retry
    has to leave the same row count as one clean load."""
    torn = tmp_path / "torn.duckdb"
    clean = tmp_path / "clean.duckdb"

    tear_the_load(slice_dir, torn)
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(torn)]) == 0
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(clean)]) == 0

    for table in TABLE_MAP.values():
        assert table_count(torn, table) == table_count(clean, table), table


def test_marker_is_written_once_per_slice(slice_dir, tmp_path) -> None:
    db = tmp_path / "repeat.duckdb"
    for _ in range(3):
        assert main(["--slice", str(slice_dir), "--duckdb-path", str(db)]) == 0

    con = duckdb.connect(str(db))
    try:
        rows = con.execute(
            f"select count(*) from {RAW_SCHEMA}.{MANIFEST_TABLE} where slice_date = ?",
            [WINDOW[0]],
        ).fetchone()[0]
    finally:
        con.close()
    assert rows == 1


def test_empty_slice_is_marked_complete(tmp_path) -> None:
    """
    A day with no orders is finished, not unfinished.

    It leaves the tables untouched -- which is also what an interruption before
    the first table leaves -- so without a marker the two are indistinguishable
    and the DAG's verify gate would fail every quiet day in the 2018 tail.
    """
    empty = (date(2018, 9, 30), date(2018, 10, 1))
    slice_dir = write_slice(slice_window(*empty), tmp_path / "slices", empty[0])
    db = tmp_path / "empty.duckdb"

    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db)]) == 0
    assert main(["--slice", str(slice_dir), "--duckdb-path", str(db), "--verify"]) == 0

    marker = duckdb_marker(empty[0], db)
    assert marker is not None
    assert marker["table_count"] == 0


def test_reconcile_reports_a_table_that_was_never_loaded(slice_dir) -> None:
    """
    The unit-level statement of the same bug.

    `reconcile` is given counts for four of the five tables the slice owes -- the
    shape an interrupted load produces. The check it replaced guarded
    `table in counts` and so found nothing to complain about.
    """
    manifest = {"row_counts": dict.fromkeys(TABLE_MAP, 0)}
    counts = dict.fromkeys(list(TABLE_MAP.values())[:-1], 0)

    problems = reconcile(slice_dir, manifest, counts)

    assert len(problems) == 1
    assert TABLE_MAP["order_reviews"] in problems[0]
    assert "never loaded" in problems[0]


def test_reconcile_accepts_a_slice_with_no_order_items(tmp_path) -> None:
    """
    Sparse days in the 2018 tail are complete with four tables, not five.

    2018-10-01 is one real order with no items, so replay writes no
    order_items.parquet and the warehouse owes no order_items load.
    """
    slice_dir = tmp_path / "purchase_date=2018-10-01"
    slice_dir.mkdir()
    for stem in TABLE_MAP:
        if stem != "order_items":
            (slice_dir / f"{stem}.parquet").write_bytes(b"")

    manifest = {"row_counts": {"orders": 1, "order_items": 0}}
    counts = {table: 1 for stem, table in TABLE_MAP.items() if stem != "order_items"}

    assert reconcile(slice_dir, manifest, counts) == []
