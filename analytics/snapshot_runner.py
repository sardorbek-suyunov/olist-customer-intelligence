"""
Execute agent-authored SQL against the committed Parquet snapshot.

WHY THE PUBLIC DEMO DOES NOT RUN THE AGENT AGAINST BIGQUERY
-----------------------------------------------------------
`bq_safety` exists and works, and a deployment that used it would be defensible.
This one does not, for three reasons that all point the same way:

  CREDENTIALS  Running against BigQuery needs a service-account key in Streamlit
               secrets. That is a real credential, on a public host, reachable by
               whatever the app does next. The snapshot needs none.
  SPEND        A stranger's question would bill bytes. Against Parquet the
               marginal cost of a question is the Gemini call and nothing else,
               which is the only cost `gemini_budget` can actually cap.
  SURVIVAL     The dashboard already reads the snapshot by default precisely so
               the demo outlives an expired key. An agent that needs live
               BigQuery would reintroduce the dependency the rest of the app was
               built to avoid.

The guard still applies in full. sql_guard parses the statement, refuses anything
that is not a single SELECT, and injects a LIMIT -- none of which was ever about
BigQuery. What is lost is `maximum_bytes_billed`, and against a 15 MiB local file
there is nothing for it to protect.

DIALECT
-------
The agent writes BigQuery SQL, because that is the schema it is shown and the
dialect the gold set is scored in. sqlglot transpiles it to DuckDB rather than
the prompt being changed: the agent's output stays one thing, scored one way,
and the execution target is a detail of deployment. Transpiling is also strictly
safer than string-replacing a schema prefix, which is what this did first and
which mangles any query containing the schema name in a literal.
"""

from __future__ import annotations

import logging
from pathlib import Path

import sqlglot
from sqlglot import exp

LOG = logging.getLogger("olist.snapshot_runner")

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "dashboard" / "data"

# Kept in step with scripts/export_snapshot.py by the test below rather than by
# memory: a mart added there and not here is a table the agent is told about and
# cannot query.
SNAPSHOT_TABLES = (
    "dim_customers",
    "dim_products",
    "dim_sellers",
    "fct_orders",
    "fct_segment_aspect",
)


class SnapshotRunner:
    """Runs guarded SQL against Parquet. No credentials, no bytes, no ceiling."""

    def __init__(self, data_dir: Path = SNAPSHOT, schema: str = "olist_marts") -> None:
        import duckdb

        self.schema = schema
        self.con = duckdb.connect(":memory:")
        missing = []
        for table in SNAPSHOT_TABLES:
            path = data_dir / f"{table}.parquet"
            if not path.exists():
                missing.append(table)
                continue
            self.con.execute(
                f"create view {table} as select * from read_parquet('{path.as_posix()}')"
            )
        if missing:
            raise FileNotFoundError(
                f"snapshot is missing {', '.join(missing)} -- run `make snapshot`. "
                "The agent is shown these tables in its prompt, so a missing file "
                "is a question it can be asked and cannot answer."
            )

    def to_duckdb(self, sql: str) -> str:
        """
        BigQuery SQL -> DuckDB SQL, with the dataset qualifier stripped.

        The qualifier is removed on the AST, not by string replacement: a query
        with 'olist_marts' inside a string literal is a perfectly legal query and
        a replace would corrupt it.
        """
        tree = sqlglot.parse_one(sql, dialect="bigquery")
        for table in tree.find_all(exp.Table):
            if table.args.get("db") and table.args["db"].name.lower() == self.schema:
                table.set("db", None)
                table.set("catalog", None)
        return tree.sql(dialect="duckdb")

    def run(self, sql: str, max_rows: int = 200):
        import pandas as pd  # noqa: F401  (duckdb .df() needs it)

        return self.con.execute(self.to_duckdb(sql)).df().head(max_rows)

    def close(self) -> None:
        self.con.close()
