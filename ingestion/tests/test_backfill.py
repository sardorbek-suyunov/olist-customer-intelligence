"""Tests for the backfill window planner. Pure date arithmetic, no I/O."""

from __future__ import annotations

from datetime import date, timedelta

from ingestion.backfill import DAILY_HANDOVER, windows
from ingestion.replay import DATA_COVERAGE_END, DATA_COVERAGE_START

FULL = (DATA_COVERAGE_START, DATA_COVERAGE_END + timedelta(days=1))


def test_full_coverage_is_bounded() -> None:
    """~56 windows, not ~775. The whole reason the schedule is mixed-grain."""
    plan = windows(*FULL)
    assert 50 <= len(plan) <= 60, f"expected ~56 windows, got {len(plan)}"


def test_windows_are_contiguous_and_non_overlapping() -> None:
    """Consecutive windows must partition the timeline exactly once."""
    plan = windows(*FULL)
    for (_, end), (nxt, _) in zip(plan, plan[1:], strict=False):
        assert end == nxt, f"gap or overlap at {end} -> {nxt}"


def test_windows_cover_the_whole_range() -> None:
    """
    Coverage must be a superset, not an exact match. Monthly windows snap to
    the 1st, so the first one opens 2016-09-01 while the data starts
    2016-09-04. Replaying the three empty days is harmless; leaving a gap
    would silently drop orders.
    """
    plan = windows(*FULL)
    assert plan[0][0] <= FULL[0], "first window must not start after the data does"
    assert plan[-1][1] >= FULL[1], "last window must not end before the data does"


def test_monthly_before_handover_daily_after() -> None:
    for start, end in windows(*FULL):
        span = (end - start).days
        if start < DAILY_HANDOVER:
            assert span > 1, f"{start} should be a monthly window, spans {span}d"
        else:
            assert span == 1, f"{start} should be a daily window, spans {span}d"


def test_subrange_entirely_before_handover_is_all_monthly() -> None:
    plan = windows(date(2017, 1, 1), date(2017, 4, 1))
    assert len(plan) == 3
    assert all((e - s).days > 1 for s, e in plan)
