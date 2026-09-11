"""
Tests for the replay harness.

These need the raw CSVs, so they skip cleanly when archive/ is absent (a fresh
clone before `make data`).
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from ingestion.replay import (
    DATA_COVERAGE_END,
    RAW,
    main,
    slice_window,
    write_slice,
)

pytestmark = pytest.mark.skipif(
    not (RAW / "olist_orders_dataset.csv").exists(),
    reason="raw dataset not present; run `make data`",
)


def test_window_is_half_open() -> None:
    """
    Consecutive slices must partition the timeline exactly once. If the window
    were closed, an order purchased at a boundary would land in both slices
    and be double counted.
    """
    march = slice_window(date(2017, 3, 1), date(2017, 4, 1))
    april = slice_window(date(2017, 4, 1), date(2017, 5, 1))

    overlap = set(march["orders"].order_id) & set(april["orders"].order_id)
    assert not overlap, f"{len(overlap)} orders appear in two slices"


def test_slice_is_referentially_consistent() -> None:
    """Every child row in a slice must point at an order inside that slice."""
    tables = slice_window(date(2017, 3, 1), date(2017, 4, 1))
    order_ids = set(tables["orders"].order_id)

    for name in ("order_items", "order_payments", "order_reviews"):
        assert tables[name].order_id.isin(order_ids).all(), f"{name} escapes the slice"

    assert tables["orders"].customer_id.isin(set(tables["customers"].customer_id)).all()


def test_customers_are_one_to_one_with_orders() -> None:
    """The fact that makes dbt snapshot unusable, asserted at the boundary."""
    tables = slice_window(date(2017, 3, 1), date(2017, 4, 1))
    orders, customers = tables["orders"], tables["customers"]
    assert len(orders) == len(customers) == orders.customer_id.nunique()


def test_rerunning_a_slice_is_idempotent(tmp_path) -> None:
    """A retried Airflow task must not duplicate rows."""
    window = (date(2017, 3, 1), date(2017, 4, 1))
    tables = slice_window(*window)

    first = write_slice(tables, tmp_path, window[0])
    sizes_first = {p.name: p.stat().st_size for p in first.glob("*.parquet")}

    second = write_slice(slice_window(*window), tmp_path, window[0])
    sizes_second = {p.name: p.stat().st_size for p in second.glob("*.parquet")}

    assert first == second
    assert sizes_first == sizes_second


def test_window_beyond_coverage_exits_zero(tmp_path, caplog) -> None:
    """
    ADR 0002: past the end of the extract is a no-op, not a failure -- and it
    must say why, since a silent skip is indistinguishable from a broken DAG.
    """
    beyond = date(DATA_COVERAGE_END.year + 5, 1, 1)
    with caplog.at_level(logging.INFO, logger="olist.replay"):
        code = main(
            [
                "--start",
                beyond.isoformat(),
                "--end",
                beyond.replace(day=2).isoformat(),
                "--out",
                str(tmp_path),
            ]
        )
    assert code == 0
    assert "beyond the Olist coverage window" in caplog.text
    assert not list(tmp_path.iterdir()), "no slice should be written past coverage"


def test_inverted_window_is_rejected(tmp_path) -> None:
    code = main(
        [
            "--start",
            "2017-04-01",
            "--end",
            "2017-03-01",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 2
