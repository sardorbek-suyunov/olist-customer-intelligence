"""
Parse agent-authored SQL and decide whether it may run.

`bq_safety.assert_read_only` does this with a regex. That was the right tool for
a belt-and-braces check behind IAM, and it is the wrong tool for the primary
gate, because a regex reads text and SQL is a grammar. Two examples it cannot
distinguish without becoming a parser itself:

    select 'delete from x' as note from t        -- harmless, the word is a string
    select * from t where c = 'a; drop table y'  -- harmless, a semicolon in a literal

The first would be rejected and the second would trip the multi-statement check,
so the regex is wrong in the safe direction -- but "wrong in the safe direction"
still means an agent that writes correct SQL gets told no, retries, and burns
budget on a query that was always fine.

So the statement is parsed with sqlglot into an AST, and every decision is made
against nodes rather than against substrings. The regex stays where it is, as a
second opinion: this module is defence in depth on top of IAM, not a replacement
for it. The service account that runs these queries holds
roles/bigquery.dataViewer on the marts dataset and nothing else, which is what
actually stops a DELETE -- a parser can only stop one it recognises.

LIMIT IS INJECTED, NOT REQUIRED
-------------------------------
An agent told "always add a LIMIT" will usually add one. Usually is not a
control. Rather than rejecting a statement that lacks one and spending a retry
teaching the model a lesson it has already been told, the limit is added to the
AST and the rewritten SQL is what runs. An existing smaller limit is left alone;
a larger one is lowered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

LOG = logging.getLogger("olist.sql_guard")

DIALECT = "bigquery"

# Node types that write, change structure, or reach outside a read. Checked by
# type against the parsed tree, so a string literal that happens to contain the
# word "delete" is not a finding.
#
# exp.Copy is here because it was NOT, and that was a real hole rather than a
# theoretical one: `COPY (SELECT * FROM fct_orders) TO '/app/x.csv'` passed this
# guard and wrote 53 KB to disk. COPY is not DML, so a denylist of writing
# statement types did not contain it -- which is the argument for the function
# allowlist below rather than a longer denylist.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Set,
    exp.Pragma,
    exp.Use,
    exp.Command,
)

# Functions a question about this warehouse can legitimately need.
#
# AN ALLOWLIST, NOT A DENYLIST, and the difference is the whole point. A denylist
# has to name read_csv, read_text, read_blob, read_json, getenv, COPY's variants,
# every extension's entry points, and whatever the next DuckDB release adds -- it
# is a list of the attacks somebody has already thought of. This list is instead
# the vocabulary the demo's own queries use, and anything outside it is refused
# whether or not it is known to be dangerous.
#
# Derived from what the gold set and the worked examples actually call, plus
# ordinary analytic SQL. analytics/tests/test_sql_guard_functions.py asserts that
# every one of the 25 gold questions and 6 demo examples still passes, so this
# cannot silently narrow until the agent stops working.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # aggregates
        "avg",
        "count",
        "max",
        "min",
        "sum",
        "stddev",
        "variance",
        "approx_distinct",
        "any_value",
        "median",
        "mode",
        "percentile_cont",
        # windows and ordering
        "row_number",
        "rank",
        "dense_rank",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "over",
        # null handling and conditionals
        "coalesce",
        "ifnull",
        "nullif",
        "if",
        "iif",
        "case",
        "greatest",
        "least",
        # numeric
        "abs",
        "ceil",
        "ceiling",
        "floor",
        "round",
        "trunc",
        "power",
        "sqrt",
        "exp",
        "ln",
        "log",
        "log10",
        "mod",
        "sign",
        "safe_divide",
        "div",
        # strings
        "concat",
        "lower",
        "upper",
        "trim",
        "ltrim",
        "rtrim",
        "length",
        "substr",
        "substring",
        "replace",
        "split",
        "starts_with",
        "ends_with",
        "contains",
        "left",
        "right",
        "lpad",
        "rpad",
        "format",
        "regexp_contains",
        "regexp_extract",
        "regexp_replace",
        "string_agg",
        "array_agg",
        # dates and times
        "current_date",
        "current_timestamp",
        "date",
        "datetime",
        "timestamp",
        "date_add",
        "date_sub",
        "date_diff",
        "date_trunc",
        "extract",
        "format_date",
        "parse_date",
        "unix_date",
        "year",
        "month",
        "day",
        "quarter",
        "week",
        "dayofweek",
        "hour",
        "minute",
        "second",
        "timestamp_diff",
        "timestamp_trunc",
        "date_part",
        # casts and types
        "cast",
        "safe_cast",
        "to_char",
        "cast_to_type",
    }
)

DEFAULT_MAX_ROWS = 200


class UnsafeSQL(ValueError):
    """The statement will not be run, and the message says precisely why."""


@dataclass(frozen=True)
class Checked:
    """A statement that passed, with the tables it touches and the SQL to run."""

    sql: str
    tables: tuple[str, ...]
    limit_added: bool


def _table_name(node: exp.Table) -> str:
    """`project.dataset.table` -> `dataset.table`, lowercased."""
    parts = [p.name for p in (node.args.get("catalog"), node.args.get("db"), node.this) if p]
    return ".".join(parts[-2:]).lower() if len(parts) > 1 else parts[-1].lower()


def _disallowed_functions(tree: exp.Expression) -> set[str]:
    """
    Every function called in the tree that is not on the allowlist.

    sqlglot parses a function it recognises into its own typed node -- SUM
    becomes exp.Sum -- and anything it does not recognise into exp.Anonymous
    carrying the raw name. Both are checked, because recognising a name is not
    the same as approving it: a dialect function sqlglot knows perfectly well
    can still be one this warehouse has no business running.

    `getenv('GEMINI_API_KEY')` is the case that motivated this. It parsed as
    Anonymous, passed every check this module had, and reached DuckDB -- where
    it failed only because DuckDB 1.5.5 has no such function. That is luck, not
    a control, and it is a version away from stopping being true.
    """
    found: set[str] = set()
    for node in tree.find_all(exp.Func):
        # AND and OR subclass Func in sqlglot but are grammar, not calls. They
        # carry no arguments a caller chooses and there is nothing to allow or
        # deny about them; listing them would only make the allowlist read as
        # though boolean logic were a privilege.
        if isinstance(node, exp.Connector):
            continue
        name = (node.sql_name() if hasattr(node, "sql_name") else type(node).__name__).lower()
        if isinstance(node, exp.Anonymous):
            name = str(node.this).lower()
        if name not in ALLOWED_FUNCTIONS:
            found.add(name)
    return found


def check(
    sql: str,
    allowed_tables: set[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> Checked:
    """
    Parse, validate, and return the SQL that should actually be executed.

    `allowed_tables` is a convenience for tests and for a clearer error message,
    NOT the security boundary. The boundary is the service account's IAM grant:
    an allowlist in application code protects nothing once the application is
    the thing that is wrong.
    """
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:  # noqa: BLE001 - the parse error IS the message
        raise UnsafeSQL(f"Could not parse as BigQuery SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if not statements:
        raise UnsafeSQL("Empty statement")
    if len(statements) > 1:
        raise UnsafeSQL(f"Expected one statement, parsed {len(statements)}")

    tree = statements[0]

    for node_type in FORBIDDEN_NODES:
        found = tree.find(node_type)
        if found is not None:
            raise UnsafeSQL(f"{node_type.__name__.upper()} is not permitted; this is read-only")

    # A UNION or a WITH parses to its own root node, so rather than enumerating
    # every selectable type, require that a SELECT is reachable at all.
    if tree.find(exp.Select) is None:
        raise UnsafeSQL(f"Statement must be a SELECT, parsed {type(tree).__name__}")

    unknown_functions = sorted(_disallowed_functions(tree))
    if unknown_functions:
        raise UnsafeSQL(
            f"Function(s) not on the allowlist: {', '.join(unknown_functions)}. "
            "This warehouse answers questions with ordinary analytic SQL; anything "
            "else is refused whether or not it is known to be dangerous."
        )

    # CTE names look exactly like tables in the tree and are not tables. Without
    # this, `with x as (...) select from x` is rejected for referencing a table
    # outside the marts -- the agent writes perfectly good SQL, gets refused,
    # and retries into the same wall.
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = tuple(sorted({_table_name(t) for t in tree.find_all(exp.Table)} - cte_names))
    if not tables:
        raise UnsafeSQL("Statement references no tables")

    if allowed_tables is not None:
        unknown = [t for t in tables if t not in allowed_tables]
        if unknown:
            raise UnsafeSQL(
                f"References table(s) outside the marts: {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(allowed_tables))}"
            )

    # LIMIT, on the outermost select only. A limit pushed into a CTE would change
    # what the query means rather than how much of it comes back.
    limit_added = False
    outer = tree.find(exp.Select) if not isinstance(tree, exp.Select) else tree
    existing = outer.args.get("limit") if outer is not None else None
    if outer is not None:
        if existing is None:
            outer.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
            limit_added = True
        else:
            try:
                current = int(existing.expression.name)
                if current > max_rows:
                    outer.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
                    limit_added = True
            except (AttributeError, ValueError):
                # A non-literal limit (a parameter, an expression) cannot be
                # compared, so it is replaced rather than trusted.
                outer.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
                limit_added = True

    return Checked(sql=tree.sql(dialect=DIALECT), tables=tables, limit_added=limit_added)
