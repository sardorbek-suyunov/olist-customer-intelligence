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

WHAT MOVING TO DUCKDB ACTUALLY COST, WHICH WAS NOT NOTHING
----------------------------------------------------------
An earlier version of this docstring said the only thing lost was
`maximum_bytes_billed`, and that against a 15 MiB local file there was nothing
for it to protect. That was wrong in a way worth recording, because it is the
same mistake as everything else in this repository's failure table: it described
the control that was removed and not the control that was doing the work.

The real boundary on BigQuery was IAM. The service account held
roles/bigquery.dataViewer on one dataset and nothing else, so a statement the
parser failed to recognise still could not read a file, write a file, or reach
the network -- there was no filesystem to reach and no permission to use it.
Moving to DuckDB did not weaken that guarantee. It removed it, and put the
process's own filesystem privileges where it used to be.

DuckDB permits a great deal that is not DML, so a statement-type check does not
cover the gap. Measured against this code, not assumed:

    COPY (SELECT * FROM fct_orders) TO '/app/x.csv'   -- guard ALLOWED it, and it
                                                      -- wrote 53 KB to disk
    SELECT * FROM read_csv('/tmp/.env')               -- DuckDB returns the file's
                                                      -- contents; refused here
                                                      -- only by the table
                                                      -- allowlist, which
                                                      -- sql_guard's own docstring
                                                      -- called "NOT the security
                                                      -- boundary"

So the execution environment is layered underneath the parser, and the parser is
no longer the only thing between a visitor and the filesystem:

  read_only=True              the connection cannot create, write or attach.
  enable_external_access=off  no file reads, no file writes, no extension
                              installs, no HTTP. This is the one that matters:
                              it is what turns read_csv and COPY from "the guard
                              had better catch that" into "the engine refuses".
  lock_configuration=true     set immediately after, so a query cannot turn the
                              previous line back on.
  timeout                     a pathological join is interrupted rather than
                              hanging the app for every other visitor.

TABLES ARE MATERIALISED, NOT VIEWS OVER PARQUET
-----------------------------------------------
This used to create views wrapping `read_parquet(...)`, which means every query
touched the filesystem at execution time. With external access disabled those
views stop working -- the snapshot's own Parquet is a file like any other. So
the data is copied into the database first and the door is shut afterwards. The
whole snapshot is ~15 MiB, so the cost of holding it is nothing, and the benefit
is that no legitimate query needs file access at all, which is what makes
disabling it a real option rather than a trade.

What is genuinely lost with BigQuery is `maximum_bytes_billed`, and against a
local file there is nothing for it to bill.

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


# A query is interrupted after this many seconds. The demo's real queries are
# aggregations over at most 99,441 rows and finish in milliseconds; this is sized
# to be unreachable by anything legitimate and reachable by a cartesian join.
DEFAULT_TIMEOUT_SECONDS = 10.0


class QueryTimeout(RuntimeError):
    """The statement ran past the deadline and was interrupted."""


class SnapshotRunner:
    """Runs guarded SQL against Parquet. No credentials, no bytes, no ceiling."""

    def __init__(
        self,
        data_dir: Path = SNAPSHOT,
        schema: str = "olist_marts",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        import tempfile

        import duckdb

        self.schema = schema
        self.timeout_seconds = timeout_seconds

        missing = [t for t in SNAPSHOT_TABLES if not (data_dir / f"{t}.parquet").exists()]
        if missing:
            raise FileNotFoundError(
                f"snapshot is missing {', '.join(missing)} -- run `make snapshot`. "
                "The agent is shown these tables in its prompt, so a missing file "
                "is a question it can be asked and cannot answer."
            )

        # A file rather than :memory: because read_only is the point and an
        # in-memory database cannot be opened read-only -- there would be nothing
        # in it. Written once here, then never written again.
        self._dir = tempfile.mkdtemp(prefix="olist_snapshot_")
        self._path = Path(self._dir) / "snapshot.duckdb"

        loader = duckdb.connect(str(self._path))
        try:
            for table in SNAPSHOT_TABLES:
                path = data_dir / f"{table}.parquet"
                loader.execute(
                    f"create table {table} as select * from read_parquet('{path.as_posix()}')"
                )
        finally:
            loader.close()

        # From here the connection can read the snapshot and nothing else.
        self.con = duckdb.connect(str(self._path), read_only=True)
        self.con.execute("set enable_external_access = false")
        # Immediately after, and not one statement later: between those two lines
        # a query could turn external access back on.
        self.con.execute("set lock_configuration = true")
        self._assert_locked_down()

    def _assert_locked_down(self) -> None:
        """
        Prove the controls are on by trying to defeat them, here, at startup.

        Asserting that a `SET` statement did not raise only establishes that
        DuckDB accepted the words. The question is whether the setting does
        anything, and the only answer that means something is a refusal -- the
        same reason `maximum_bytes_billed` was not believed until BigQuery was
        seen rejecting a query with it.
        """
        probes = [
            ("read a file", f"select * from read_csv('{self._path.as_posix()}')"),
            ("re-enable external access", "set enable_external_access = true"),
            ("unlock the configuration", "set lock_configuration = false"),
            ("write to the database", "create table _probe as select 1"),
        ]
        for what, sql in probes:
            try:
                self.con.execute(sql)
            except Exception:  # noqa: BLE001 - a refusal is the pass condition
                continue
            raise RuntimeError(
                f"the snapshot connection is not locked down: it permitted {what!r}. "
                "Refusing to serve queries -- this connection would give a visitor "
                "the filesystem privileges of the process."
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
        """
        Execute, under a deadline, and return at most `max_rows`.

        The row cap is applied with `fetchmany` rather than by materialising the
        full result and slicing it. sql_guard injects a LIMIT, so a well-formed
        statement is already bounded -- but a join that multiplies before it
        limits can still produce an enormous intermediate, and `.df().head(n)`
        would build the whole frame in memory first and only then throw it away.
        """
        import threading

        import pandas as pd

        statement = self.to_duckdb(sql)

        # DuckDB has no statement timeout, so the deadline is a timer that calls
        # interrupt() on the connection. Without this one pathological query
        # hangs the process for every other visitor -- the demo is a single
        # shared connection, so a slow query is not a private problem.
        timer = threading.Timer(self.timeout_seconds, self.con.interrupt)
        timer.start()
        try:
            cursor = self.con.execute(statement)
            rows = cursor.fetchmany(max_rows)
            columns = [d[0] for d in cursor.description]
        except Exception as exc:  # noqa: BLE001 - re-raised, classified
            if "INTERRUPT" in str(exc).upper() or type(exc).__name__ == "InterruptException":
                raise QueryTimeout(
                    f"Query exceeded {self.timeout_seconds:.0f}s and was cancelled."
                ) from exc
            raise
        finally:
            timer.cancel()

        return pd.DataFrame(rows, columns=columns)

    def close(self) -> None:
        import shutil

        self.con.close()
        shutil.rmtree(self._dir, ignore_errors=True)
