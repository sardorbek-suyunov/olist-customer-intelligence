"""
The two session ceilings must be able to bind at the same time.

`bq_safety` caps a session two ways: a cumulative BYTE budget and a QUERY count.
Both are real controls only if each one can actually be the thing that stops a
session. If the query count is higher than the byte budget can ever afford, the
count never fires -- it is a number in a dataclass with nothing behind it, and
worse, it advertises a capacity the session does not have.

That is what happened. The count was 50 and the byte budget affords 11 of the
most expensive query the demo can issue, so a visitor was promised 50 questions
and cut off at 11 by a different ceiling with a different error message.

These tests are written against the MEASURED cost of that query shape, recorded
by scripts/measure_vector_search_bytes.py from an executed query. So a future
change to the embedding dimensionality -- the obvious thing that would move it --
cannot silently reintroduce the gap: the measurement file moves, and this fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analytics.bq_safety import (
    DEFAULT_MAX_BYTES_PER_QUERY,
    DEFAULT_MAX_BYTES_PER_SESSION,
    DEFAULT_MAX_QUERIES_PER_SESSION,
    MEASURED_WORST_QUERY_BYTES,
    BudgetExceeded,
    QueryBudget,
)

MEASUREMENT = (
    Path(__file__).resolve().parents[2] / "enrichment" / "eval" / "vector_search_bytes.json"
)


def test_the_worst_query_fits_under_the_per_query_ceiling() -> None:
    """If it does not, the demo's headline feature cannot run at all."""
    assert MEASURED_WORST_QUERY_BYTES <= DEFAULT_MAX_BYTES_PER_QUERY, (
        f"the most expensive query shape bills {MEASURED_WORST_QUERY_BYTES / 1024**2:.0f} MiB "
        f"against a {DEFAULT_MAX_BYTES_PER_QUERY / 1024**2:.0f} MiB per-query ceiling"
    )


def test_the_query_cap_is_what_the_byte_budget_affords() -> None:
    """
    Neither ceiling is decorative: the count is derived from the budget.

    Asserted rather than assumed, because the derivation is one line and one line
    is exactly what gets replaced by a round number later.
    """
    affordable = DEFAULT_MAX_BYTES_PER_SESSION // MEASURED_WORST_QUERY_BYTES
    assert affordable == DEFAULT_MAX_QUERIES_PER_SESSION
    assert DEFAULT_MAX_QUERIES_PER_SESSION >= 1, "the budget affords no queries at all"


def test_a_full_session_of_worst_case_queries_does_not_overrun_the_budget() -> None:
    """
    The property that matters, exercised rather than computed: run the maximum
    permitted number of worst-case queries and confirm the byte budget survives.
    """
    budget = QueryBudget()
    for _ in range(DEFAULT_MAX_QUERIES_PER_SESSION):
        budget.check(MEASURED_WORST_QUERY_BYTES)
        budget.record("select 1", MEASURED_WORST_QUERY_BYTES)
    assert budget.bytes_spent <= DEFAULT_MAX_BYTES_PER_SESSION


def test_both_ceilings_can_bind_and_they_bind_together() -> None:
    """
    After the permitted number of worst-case queries, the session is exhausted --
    and it does not matter which ceiling reports it, because they agree.
    """
    budget = QueryBudget()
    for _ in range(DEFAULT_MAX_QUERIES_PER_SESSION):
        budget.check(MEASURED_WORST_QUERY_BYTES)
        budget.record("select 1", MEASURED_WORST_QUERY_BYTES)
    with pytest.raises(BudgetExceeded):
        budget.check(MEASURED_WORST_QUERY_BYTES)


@pytest.mark.skipif(not MEASUREMENT.exists(), reason="no measurement recorded yet")
def test_the_constant_matches_the_recorded_measurement() -> None:
    """
    The constant is a copy of a measured number, so it can drift from it. This is
    the test that a dimension change trips: re-running the measurement writes a
    new worst-case figure, and the constant no longer matches it.
    """
    recorded = json.loads(MEASUREMENT.read_text(encoding="utf-8"))
    measured = recorded["worst_billed_bytes"]
    # Rounded to whole MiB in the constant, so allow that much slack and no more.
    assert abs(MEASURED_WORST_QUERY_BYTES - measured) < 1024**2, (
        f"bq_safety says {MEASURED_WORST_QUERY_BYTES / 1024**2:.0f} MiB but the last "
        f"measurement recorded {measured / 1024**2:.0f} MiB at "
        f"{recorded['dimensions']} dimensions. Re-run "
        "scripts/measure_vector_search_bytes.py and update the constant."
    )
    assert recorded["searches_per_session"] == DEFAULT_MAX_QUERIES_PER_SESSION
