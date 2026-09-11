"""Unit tests for the NL->SQL spend ceilings. No GCP calls."""

from __future__ import annotations

import pytest

from analytics.bq_safety import (
    GIB,
    BudgetExceeded,
    QueryBudget,
    UnsafeStatement,
    assert_read_only,
)


class TestReadOnlyGuard:
    @pytest.mark.parametrize(
        "sql",
        [
            "select 1",
            "SELECT * FROM fct_orders LIMIT 10",
            "with x as (select 1) select * from x",
            "  select 1  ;  ",
        ],
    )
    def test_accepts_reads(self, sql: str) -> None:
        assert assert_read_only(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "delete from fct_orders",
            "DROP TABLE dim_customers",
            "update dim_customers set customer_city = 'x'",
            "create or replace table t as select 1",
            "merge into t using s on t.id = s.id when matched then delete",
        ],
    )
    def test_rejects_writes(self, sql: str) -> None:
        with pytest.raises(UnsafeStatement):
            assert_read_only(sql)

    def test_rejects_stacked_statements(self) -> None:
        with pytest.raises(UnsafeStatement, match="Multiple statements"):
            assert_read_only("select 1; drop table dim_customers")

    def test_keyword_hidden_in_comment_does_not_smuggle_a_write(self) -> None:
        """Comments are stripped before the keyword scan, not after."""
        with pytest.raises(UnsafeStatement):
            assert_read_only("select 1 /* harmless */ ; delete from fct_orders")

    def test_comment_only_statement_rejected(self) -> None:
        with pytest.raises(UnsafeStatement, match="Empty"):
            assert_read_only("-- just a comment")

    def test_the_word_created_inside_an_identifier_is_not_a_write(self) -> None:
        """'created' is a legitimate order_status value in this dataset."""
        assert assert_read_only("select * from fct_orders where order_status = 'created'")


class TestQueryBudget:
    def test_rejects_query_over_per_query_ceiling(self) -> None:
        budget = QueryBudget(max_bytes_per_query=GIB)
        with pytest.raises(BudgetExceeded, match="per-query ceiling"):
            budget.check(2 * GIB)

    def test_rejects_when_session_budget_exhausted(self) -> None:
        budget = QueryBudget(max_bytes_per_query=GIB, max_bytes_per_session=2 * GIB)
        budget.record("select 1", GIB)
        budget.record("select 2", GIB)
        with pytest.raises(BudgetExceeded, match="remains in this session"):
            budget.check(1)

    def test_many_small_queries_still_exhaust_the_session(self) -> None:
        """The case a per-query cap alone cannot see."""
        budget = QueryBudget(
            max_bytes_per_query=GIB,
            max_bytes_per_session=10 * GIB,
            max_queries_per_session=1000,
        )
        for _ in range(10):
            budget.check(GIB)
            budget.record("select 1", GIB)
        with pytest.raises(BudgetExceeded):
            budget.check(GIB)

    def test_rejects_when_query_count_exhausted(self) -> None:
        budget = QueryBudget(max_queries_per_session=2)
        for _ in range(2):
            budget.record("select 1", 0)
        with pytest.raises(BudgetExceeded, match="query limit"):
            budget.check(0)

    def test_bytes_remaining_never_negative(self) -> None:
        budget = QueryBudget(max_bytes_per_session=GIB)
        budget.record("select 1", 5 * GIB)
        assert budget.bytes_remaining == 0

    def test_allows_query_inside_both_ceilings(self) -> None:
        budget = QueryBudget(max_bytes_per_query=GIB, max_bytes_per_session=5 * GIB)
        budget.check(100 * 1024**2)  # 100 MiB, fine
