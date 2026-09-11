"""
Drive replay + load across the whole coverage window.

Populates the raw tables that dbt's sources read, without needing an Airflow
scheduler. Same shape the DAGs use -- monthly for the bulk history, daily for
the tail -- so running this by hand and running the DAGs produce identical
warehouse state.

    python -m ingestion.backfill --target duckdb
    python -m ingestion.backfill --target bigquery
    python -m ingestion.backfill --start 2017-01-01 --end 2017-04-01

Idempotent end to end: every slice is delete-then-insert scoped to its own
date, so re-running a backfill converges rather than duplicating.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from ingestion import load as load_module
from ingestion import replay as replay_module

LOG = logging.getLogger("olist.backfill")

ROOT = Path(__file__).resolve().parents[1]

# Mirrors orchestration/dags/olist_batch.py. The tail is replayed daily so the
# incremental path is exercised on real data, not just asserted.
DAILY_HANDOVER = date(2018, 9, 17)


def month_starts(start: date, end: date) -> list[date]:
    out, cursor = [], start.replace(day=1)
    while cursor < end:
        out.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def windows(start: date, end: date) -> list[tuple[date, date]]:
    """Monthly up to the handover, daily after it -- 25 + ~31 rather than ~775."""
    result: list[tuple[date, date]] = []

    monthly_end = min(end, DAILY_HANDOVER)
    starts = month_starts(start, monthly_end)
    for index, first in enumerate(starts):
        nxt = starts[index + 1] if index + 1 < len(starts) else monthly_end
        if nxt > first:
            result.append((first, nxt))

    cursor = max(start, DAILY_HANDOVER)
    while cursor < end:
        result.append((cursor, cursor + timedelta(days=1)))
        cursor += timedelta(days=1)

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", type=date.fromisoformat, default=replay_module.DATA_COVERAGE_START
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=replay_module.DATA_COVERAGE_END + timedelta(days=1),
    )
    parser.add_argument("--target", default="duckdb")
    parser.add_argument("--slices", type=Path, default=ROOT / "data" / "slices")
    parser.add_argument("--duckdb-path", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    )

    plan = windows(args.start, args.end)
    monthly = sum(1 for a, b in plan if (b - a).days > 1)
    LOG.info(
        "Backfilling %s: %s windows (%s monthly + %s daily)",
        args.target,
        len(plan),
        monthly,
        len(plan) - monthly,
    )

    total_orders = 0
    for index, (first, last) in enumerate(plan, 1):
        replay_argv = [
            "--start",
            first.isoformat(),
            "--end",
            last.isoformat(),
            "--out",
            str(args.slices),
            "--log-level",
            "WARNING",
        ]
        if replay_module.main(replay_argv) != 0:
            return 1

        slice_dir = args.slices / f"purchase_date={first.isoformat()}"
        if not slice_dir.exists():
            continue

        load_argv = [
            "--slice",
            str(slice_dir),
            "--target",
            args.target,
            "--log-level",
            "WARNING",
        ]
        if args.duckdb_path:
            load_argv += ["--duckdb-path", str(args.duckdb_path)]
        if load_module.main(load_argv) != 0:
            return 1

        manifest = load_module.read_manifest(slice_dir)
        orders = manifest["row_counts"].get("orders", 0)
        total_orders += orders
        LOG.info("  [%3d/%d] %s -> %s  orders=%s", index, len(plan), first, last, f"{orders:,}")

    LOG.info("Backfill complete: %s orders across %s windows", f"{total_orders:,}", len(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
