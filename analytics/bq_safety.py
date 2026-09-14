"""
Platform-enforced spend ceilings for the NL->SQL agent.

An LLM that writes SQL will eventually write a cross join. Prompt instructions
("always add a LIMIT") are not a control -- they are a suggestion to a
non-deterministic system. This module is the control, and it has three layers
that hold regardless of what the model emits:

  1. DRY RUN FIRST. Every statement is planned before it is run, so the exact
     bytes it would scan are known in advance and a doomed query costs nothing.
  2. PER-QUERY CEILING. maximum_bytes_billed is set on the job itself.
     BigQuery rejects the job server-side if the plan exceeds it -- this is
     enforced by the platform, not by this process.
  3. PER-SESSION BUDGET. Cumulative bytes across a conversation are tracked
     and the session is cut off when exhausted. Stops the death-by-a-thousand-
     queries case that a per-query cap cannot see.

Plus a read-only guard: the statement must parse as a single SELECT/WITH.
That is a cheap belt-and-braces check on top of the real defence, which is
that the agent's service account holds only roles/bigquery.dataViewer.

No GCP calls at import time, so the whole thing is unit-testable with a fake
client.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

LOG = logging.getLogger("olist.bq_safety")

GIB = 1024**3

# Defaults sized against a ~120 MB warehouse: a legitimate analytical query
# scans single-digit MB, so 1 GiB per query is ~10x headroom and 5 GiB per
# session allows a long conversation while staying at 0.5% of the 1 TB/month
# free allowance.
DEFAULT_MAX_BYTES_PER_QUERY = 1 * GIB
DEFAULT_MAX_BYTES_PER_SESSION = 5 * GIB

# The most expensive query shape the demo can issue, MEASURED rather than
# assumed: an unindexed VECTOR_SEARCH over 35,616 x 1536-d vectors, joined to
# the review text, fct_orders and fct_segment_aspect. 435 MiB billed, executed,
# not dry-run. scripts/measure_vector_search_bytes.py, recorded in
# enrichment/eval/vector_search_bytes.json.
#
# This constant exists because the two ceilings disagreed. The query counter was
# 50 and the byte budget affords 11 of these, which makes the looser of the two
# decorative: a session would be cut off by bytes at 11 having been promised 50,
# and the cap that "limits" the session would never once have fired. A ceiling
# that cannot be the binding constraint is not a control, it is a comment.
#
# Sized from the byte budget rather than chosen: whatever the byte budget affords
# of the worst query IS the query cap. A future dimension change moves the
# measured cost, which moves the cap, and test_ceilings_are_consistent fails if
# the recorded measurement and this arithmetic ever drift apart.
MEASURED_WORST_QUERY_BYTES = 435 * 1024**2

DEFAULT_MAX_QUERIES_PER_SESSION = DEFAULT_MAX_BYTES_PER_SESSION // MEASURED_WORST_QUERY_BYTES

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"call|export|load|begin|commit)\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class BudgetExceeded(RuntimeError):
    """Raised when a query would exceed the per-query or per-session ceiling."""


class UnsafeStatement(ValueError):
    """Raised when a statement is not a single read-only SELECT."""


class _Client(Protocol):
    """The slice of google.cloud.bigquery.Client this module needs."""

    def query(self, sql: str, job_config: Any) -> Any: ...


def assert_read_only(sql: str) -> str:
    """
    Reject anything that is not a single read-only statement.

    Secondary to IAM, not a substitute for it. Comments are stripped first so
    a keyword cannot be smuggled past the check inside one.
    """
    stripped = _COMMENT.sub(" ", sql).strip().rstrip(";")

    if not stripped:
        raise UnsafeStatement("Empty statement")

    if ";" in stripped:
        raise UnsafeStatement("Multiple statements are not permitted")

    if not re.match(r"^\s*(select|with)\b", stripped, re.IGNORECASE):
        raise UnsafeStatement("Statement must begin with SELECT or WITH")

    match = _FORBIDDEN.search(stripped)
    if match:
        raise UnsafeStatement(f"Forbidden keyword in statement: {match.group(0).upper()}")

    return stripped


@dataclass
class QueryBudget:
    """
    Cumulative spend tracker for one agent session.

    Not thread-safe by design: one budget per conversation, held by whatever
    owns that conversation.
    """

    max_bytes_per_query: int = DEFAULT_MAX_BYTES_PER_QUERY
    max_bytes_per_session: int = DEFAULT_MAX_BYTES_PER_SESSION
    max_queries_per_session: int = DEFAULT_MAX_QUERIES_PER_SESSION

    bytes_spent: int = 0
    queries_run: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def bytes_remaining(self) -> int:
        return max(0, self.max_bytes_per_session - self.bytes_spent)

    def check(self, estimated_bytes: int) -> None:
        """Raise if a query of this size may not run. Call after the dry run."""
        if self.queries_run >= self.max_queries_per_session:
            raise BudgetExceeded(f"Session query limit reached ({self.max_queries_per_session})")

        if estimated_bytes > self.max_bytes_per_query:
            raise BudgetExceeded(
                f"Query would scan {_human(estimated_bytes)}, over the "
                f"per-query ceiling of {_human(self.max_bytes_per_query)}. "
                "Add a filter on a partitioned column or select fewer columns."
            )

        if estimated_bytes > self.bytes_remaining:
            raise BudgetExceeded(
                f"Query would scan {_human(estimated_bytes)} but only "
                f"{_human(self.bytes_remaining)} remains in this session's budget."
            )

    def record(self, sql: str, billed_bytes: int) -> None:
        self.bytes_spent += billed_bytes
        self.queries_run += 1
        self.history.append({"sql": sql, "billed_bytes": billed_bytes})


def _human(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


class SafeQueryRunner:
    """Runs agent-authored SQL under the three-layer ceiling described above."""

    def __init__(self, client: _Client, budget: QueryBudget | None = None) -> None:
        self.client = client
        self.budget = budget or QueryBudget()

    def estimate(self, sql: str) -> int:
        """Plan the query without running it. Returns bytes it would scan."""
        from google.cloud import bigquery

        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return int(job.total_bytes_processed or 0)

    def run(self, sql: str, max_rows: int = 1000) -> tuple[list[dict[str, Any]], int]:
        """
        Validate, price, then execute. Returns (rows, bytes_billed).

        Raises UnsafeStatement or BudgetExceeded before any billable work
        happens. The caller is expected to hand the exception message back to
        the model, which can then retry with a narrower query.
        """
        from google.cloud import bigquery

        statement = assert_read_only(sql)

        estimated = self.estimate(statement)
        self.budget.check(estimated)
        LOG.info(
            "Approved query: plan=%s, session=%s/%s",
            _human(estimated),
            _human(self.budget.bytes_spent),
            _human(self.budget.max_bytes_per_session),
        )

        job = self.client.query(
            statement,
            job_config=bigquery.QueryJobConfig(
                # The ceiling that matters. BigQuery kills the job server-side
                # if the plan exceeds it, even if the dry-run estimate was
                # stale or this process is compromised.
                maximum_bytes_billed=self.budget.max_bytes_per_query,
                use_query_cache=True,
            ),
        )

        rows = [dict(row) for row in job.result(max_results=max_rows)]
        billed = int(getattr(job, "total_bytes_billed", 0) or 0)
        self.budget.record(statement, billed)
        return rows, billed
