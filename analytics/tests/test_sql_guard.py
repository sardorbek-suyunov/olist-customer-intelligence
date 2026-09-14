"""
The SQL guard. Each test is a statement an LLM could plausibly emit.

Two halves, and both matter:

  MUST BLOCK   writes, DDL, multiple statements, tables outside the marts.
  MUST ALLOW   correct SQL that a regex-based guard gets wrong. Those are not
               cosmetic: a false rejection costs a retry, and a retry costs a
               slice of the visitor's budget to re-learn something the agent
               already did right.
"""

from __future__ import annotations

import pytest

from analytics.sql_guard import UnsafeSQL, check

MARTS = {
    "olist_marts.fct_orders",
    "olist_marts.fct_segment_aspect",
    "olist_marts.dim_customers",
}


def ok(sql: str, **kwargs):
    return check(sql, allowed_tables=MARTS, max_rows=200, **kwargs)


# ---------------------------------------------------------------------------
# Must block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "delete from olist_marts.fct_orders where 1=1",
        "update olist_marts.fct_orders set order_value = 0",
        "insert into olist_marts.fct_orders (order_id) values ('x')",
        "drop table olist_marts.fct_orders",
        "create table t as select * from olist_marts.fct_orders",
        "alter table olist_marts.fct_orders add column x int64",
        "truncate table olist_marts.fct_orders",
    ],
)
def test_writes_and_ddl_are_blocked(sql: str) -> None:
    with pytest.raises(UnsafeSQL):
        ok(sql)


def test_a_select_followed_by_a_write_is_blocked() -> None:
    """The injection shape: a valid SELECT that smuggles a second statement."""
    with pytest.raises(UnsafeSQL, match="one statement"):
        ok("select 1 from olist_marts.fct_orders; delete from olist_marts.fct_orders")


def test_tables_outside_the_marts_are_named_in_the_error() -> None:
    with pytest.raises(UnsafeSQL, match="olist_raw"):
        ok("select * from olist_raw.olist_customers_dataset")


def test_unparseable_sql_is_blocked_rather_than_passed_through() -> None:
    with pytest.raises(UnsafeSQL, match="parse"):
        ok("select from where ((( order by")


def test_a_statement_with_no_tables_is_blocked() -> None:
    """Not dangerous, but it cannot be an answer to a question about the data."""
    with pytest.raises(UnsafeSQL, match="no tables"):
        ok("select 1")


# ---------------------------------------------------------------------------
# Must allow -- the cases a regex gets wrong
# ---------------------------------------------------------------------------


def test_a_forbidden_word_inside_a_string_literal_is_not_a_write() -> None:
    """
    `select 'delete from x' as note` writes nothing. A keyword regex cannot tell
    the difference between a word and a word inside quotes; a parser can.
    """
    result = ok("select 'delete from x' as note from olist_marts.fct_orders")
    assert result.tables == ("olist_marts.fct_orders",)


def test_a_semicolon_inside_a_string_literal_is_not_a_second_statement() -> None:
    result = ok("select * from olist_marts.fct_orders where order_id = 'a; drop table y'")
    assert result.tables == ("olist_marts.fct_orders",)


def test_a_cte_name_is_not_a_table_outside_the_marts() -> None:
    """
    `with recent as (...) select * from recent` references one real table. Before
    CTE names were excluded, this was rejected for referencing a table called
    `recent` that is not in the marts -- correct SQL, refused, retry burned.
    """
    result = ok("with recent as (select * from olist_marts.fct_orders) select * from recent")
    assert result.tables == ("olist_marts.fct_orders",)


def test_nested_ctes_resolve_to_their_real_tables() -> None:
    result = ok(
        "with a as (select * from olist_marts.fct_orders), b as (select * from a) select * from b"
    )
    assert result.tables == ("olist_marts.fct_orders",)


def test_a_union_is_allowed() -> None:
    result = ok(
        "select order_id from olist_marts.fct_orders "
        "union all select order_id from olist_marts.fct_orders"
    )
    assert result.tables == ("olist_marts.fct_orders",)


# ---------------------------------------------------------------------------
# LIMIT is injected, not demanded
# ---------------------------------------------------------------------------


def test_a_missing_limit_is_added_rather_than_rejected() -> None:
    result = ok("select rfm_segment from olist_marts.fct_segment_aspect")
    assert result.limit_added is True
    assert "LIMIT 200" in result.sql.upper()


def test_an_existing_smaller_limit_is_left_alone() -> None:
    result = ok("select * from olist_marts.fct_orders limit 5")
    assert result.limit_added is False
    assert "LIMIT 5" in result.sql.upper()


def test_a_larger_limit_is_lowered_to_the_cap() -> None:
    result = ok("select * from olist_marts.fct_orders limit 100000")
    assert result.limit_added is True
    assert "LIMIT 200" in result.sql.upper()


def test_a_non_literal_limit_is_replaced_rather_than_trusted() -> None:
    """`limit @n` cannot be compared to the cap, so it does not get the benefit."""
    result = ok("select * from olist_marts.fct_orders limit @n")
    assert result.limit_added is True
    assert "LIMIT 200" in result.sql.upper()


def test_the_returned_sql_is_what_should_run() -> None:
    """
    The guard rewrites. A caller that validates one string and executes another
    has a guard in name only, so the checked SQL is the product, not a verdict.
    """
    result = ok("select rfm_segment from olist_marts.fct_segment_aspect")
    assert result.sql != "select rfm_segment from olist_marts.fct_segment_aspect"
    assert result.sql.upper().startswith("SELECT")
