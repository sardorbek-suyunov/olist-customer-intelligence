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
