"""
Replay the static Olist extract as a time-ordered stream of slices.

The point of this module is that the pipeline is genuinely incremental rather
than a one-shot CSV load. Given a date window it emits the orders purchased in
that window plus every related row, referentially consistent, as Parquet.

Deliberately a plain CLI with no Airflow import, so the orchestrator is a thin
caller and stays swappable:

    python -m ingestion.replay --start 2017-03-01 --end 2017-04-01
    python -m ingestion.replay --start 2018-09-17 --end 2018-09-18 --out data/slices

Coverage is 2016-09-04 .. 2018-10-17. A window entirely outside that range
exits 0 having written an empty slice and logged why -- see ADR 0002.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

LOG = logging.getLogger("olist.replay")

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "archive"

# Inclusive bounds of the source extract.
DATA_COVERAGE_START = date(2016, 9, 4)
DATA_COVERAGE_END = date(2018, 10, 17)

# Child tables and the column that ties them back to an order.
ORDER_CHILDREN = {
    "order_items": "order_id",
    "order_payments": "order_id",
    "order_reviews": "order_id",
}


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(RAW / f"olist_{name}_dataset.csv")


def slice_window(start: date, end: date) -> dict[str, pd.DataFrame]:
    """
    Return every row belonging to orders purchased in [start, end).

    Half-open, matching the SCD2 validity intervals in dim_customers, so that
    consecutive slices partition the timeline exactly once with no double
    counting at the boundary.
    """
    orders = _read("orders")
    orders["order_purchase_timestamp"] = pd.to_datetime(orders["order_purchase_timestamp"])

    purchased = orders["order_purchase_timestamp"].dt.date
    in_window = (purchased >= start) & (purchased < end)
    sliced_orders = orders.loc[in_window].copy()

    if sliced_orders.empty:
        LOG.warning(
            "No orders purchased in [%s, %s). Coverage is %s .. %s.",
            start,
            end,
            DATA_COVERAGE_START,
            DATA_COVERAGE_END,
        )
        return {"orders": sliced_orders}

    order_ids = set(sliced_orders["order_id"])
    tables: dict[str, pd.DataFrame] = {"orders": sliced_orders}

    for name, key in ORDER_CHILDREN.items():
        child = _read(name)
        tables[name] = child.loc[child[key].isin(order_ids)].copy()

    # Customers are order-scoped, so a slice carries exactly its own rows.
    customers = _read("customers")
    tables["customers"] = customers.loc[
        customers["customer_id"].isin(set(sliced_orders["customer_id"]))
    ].copy()

    return tables


def write_slice(tables: dict[str, pd.DataFrame], out_dir: Path, logical_date: date) -> Path:
    """Write one Hive-partitioned slice plus a manifest, idempotently."""
    partition = out_dir / f"purchase_date={logical_date.isoformat()}"
    partition.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, int] = {}
    for name, frame in tables.items():
        # Overwrite rather than append: re-running a slice must be a no-op,
        # not a duplication.
        frame.to_parquet(partition / f"{name}.parquet", index=False)
        manifest[name] = len(frame)

    (partition / "_manifest.json").write_text(
        json.dumps(
            {
                "logical_date": logical_date.isoformat(),
                "generated_at": datetime.now(UTC).isoformat(),
                "row_counts": manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return partition


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "slices")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    )

    if args.end <= args.start:
        LOG.error("--end (%s) must be after --start (%s)", args.end, args.start)
        return 2

    if args.start > DATA_COVERAGE_END:
        # Not an error. See ADR 0002 -- the source is a static extract and the
        # scheduled pipeline is expected to reach the end of it.
        LOG.info(
            "Window [%s, %s) is beyond the Olist coverage window (%s .. %s). "
            "Nothing to ingest; this is expected for a static historical extract.",
            args.start,
            args.end,
            DATA_COVERAGE_START,
            DATA_COVERAGE_END,
        )
        return 0

    tables = slice_window(args.start, args.end)
    partition = write_slice(tables, args.out, args.start)

    LOG.info(
        "Wrote %s: %s",
        partition,
        ", ".join(f"{name}={len(frame):,}" for name, frame in tables.items()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
