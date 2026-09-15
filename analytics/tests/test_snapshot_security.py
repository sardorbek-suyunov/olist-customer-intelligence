"""
The public demo's SQL path, attacked.

WHY THIS FILE EXISTS
--------------------
The security model was designed for BigQuery, where the real boundary was IAM:
the service account held roles/bigquery.dataViewer on one dataset and nothing
else, so a statement the parser failed to recognise still could not read a file,
write a file, or reach the network. There was no filesystem to reach.

The demo now runs on DuckDB over committed Parquet. That did not weaken the IAM
guarantee, it REMOVED it, and put the process's own filesystem privileges where
it used to be. DuckDB permits a great deal that is not DML, so a statement-type
check does not cover the gap.

Measured against the code as it stood, not assumed:

  COPY (SELECT * FROM fct_orders) TO '<path>'  -- the guard ALLOWED it and it
                                               -- wrote 53 KB to disk
  SELECT * FROM read_csv('<path>')             -- DuckDB returns the file's
                                               -- contents. Refused only by the
                                               -- table allowlist, which
                                               -- sql_guard's own docstring
                                               -- called "NOT the security
                                               -- boundary"
  SELECT getenv('GEMINI_API_KEY')              -- reached DuckDB, and failed only
                                               -- because DuckDB 1.5.5 has no
                                               -- such function. Luck, not a
                                               -- control, and one release away
                                               -- from not being true
  INSTALL httpfs; LOAD httpfs;                 -- refused as two statements
  ATTACH 'https://.../x.db'                    -- refused by the BigQuery parser

Three of those five were refused for reasons that had nothing to do with anyone
deciding they should be. A control that happens to catch an attack is not the
same as a control that was built to.

WHAT THESE TESTS ASSERT
-----------------------
Not that a setting was accepted -- that a query is REFUSED, with the refusal in
the failure message. The same standard `maximum_bytes_billed` was held to: it
was not believed until BigQuery was seen rejecting a query with it, because a
configuration option that silently does nothing looks exactly like one that
works.
"""

from __future__ import annotations

import duckdb
import pytest

from analytics.snapshot_runner import SNAPSHOT_TABLES, QueryTimeout, SnapshotRunner
from analytics.sql_guard import UnsafeSQL, check

ALLOWED_TABLES = {f"olist_marts.{t}" for t in SNAPSHOT_TABLES} | set(SNAPSHOT_TABLES)


@pytest.fixture(scope="module")
def runner():
    r = SnapshotRunner()
    yield r
    r.close()


@pytest.fixture
def secret(tmp_path):
    """A readable file standing in for .env, so a refusal cannot be a format error."""
    path = tmp_path / "dotenv.csv"
    path.write_text("key,value\nGEMINI_API_KEY,sk-SUPER-SECRET-VALUE\n", encoding="utf-8")
    return path


def guarded(sql: str):
    """Exactly what dashboard/ask.py runs: the guard, with the table allowlist."""
    return check(sql, allowed_tables=ALLOWED_TABLES, max_rows=200)


# ---------------------------------------------------------------- the guard


@pytest.mark.parametrize(
    "label, sql",
    [
        ("read a local file", "SELECT * FROM read_csv('/app/.env')"),
        ("read an env var", "SELECT getenv('GEMINI_API_KEY')"),
        ("write a local file", "COPY (SELECT 1) TO '/app/x.csv'"),
        ("install an extension", "INSTALL httpfs; LOAD httpfs;"),
        ("attach a remote database", "ATTACH 'https://example.com/x.db'"),
    ],
)
def test_the_guard_refuses_each_named_attack(label, sql):
    with pytest.raises(UnsafeSQL) as excinfo:
        guarded(sql)
    assert str(excinfo.value), f"{label} was refused with an empty message"


@pytest.mark.parametrize(
    "label, sql",
    [
        # Each of these routes around the INCIDENTAL reason its parent was
        # refused, keeping the payload. Without the function allowlist and
        # exp.Copy, every one of them passed.
        (
            "getenv with a real table",
            "SELECT getenv('GEMINI_API_KEY') FROM olist_marts.fct_orders",
        ),
        (
            "getenv in a predicate",
            "SELECT order_id FROM olist_marts.fct_orders WHERE order_id = getenv('GEMINI_API_KEY')",
        ),
        (
            "getenv in a scalar subquery",
            "SELECT (SELECT getenv('GEMINI_API_KEY')) AS x FROM olist_marts.fct_orders",
        ),
        (
            "COPY with a real table",
            "COPY (SELECT * FROM olist_marts.fct_orders) TO '/app/x.csv'",
        ),
        (
            "read_text as a scalar",
            "SELECT read_text('/app/.env') FROM olist_marts.fct_orders",
        ),
        (
            "read_blob as a scalar",
            "SELECT read_blob('/app/.env') FROM olist_marts.fct_orders",
        ),
        (
            "read_csv joined to a permitted table",
            "SELECT * FROM olist_marts.fct_orders JOIN read_csv('/app/.env') ON 1=1",
        ),
    ],
)
def test_the_guard_refuses_the_ways_around_those_refusals(label, sql):
    """
    The variants are the real test.

    The five headline payloads were all refused before any of this was written,
    but three of them incidentally -- 'references no tables', 'parsed 2
    statements', a BigQuery parse error. An attacker does not send the version
    that trips an accident.
    """
    with pytest.raises(UnsafeSQL) as excinfo:
        guarded(sql)
    assert str(excinfo.value), f"{label} was refused with an empty message"


def test_copy_is_refused_as_a_statement_type():
    """COPY was the live hole: it passed the guard and wrote 53 KB to disk."""
    with pytest.raises(UnsafeSQL, match="COPY"):
        guarded("COPY (SELECT * FROM olist_marts.fct_orders) TO '/tmp/x.csv'")


def test_unknown_functions_are_refused_by_name():
    with pytest.raises(UnsafeSQL, match="allowlist"):
        guarded("SELECT getenv('X') FROM olist_marts.fct_orders")


def test_the_allowlist_still_permits_ordinary_analytic_sql():
    """
    The allowlist has to be a vocabulary, not a wall.

    An allowlist narrowed until nothing works is not secure, it is broken, and
    the failure would show up as the agent being refused rather than as an
    error anyone would look for.
    """
    checked = guarded(
        "SELECT rfm_segment, ROUND(SUM(CASE WHEN is_delivery_complaint "
        "THEN aspect_rate_of_reviewed ELSE 0 END), 4) AS rate, COUNT(*) AS n "
        "FROM olist_marts.fct_segment_aspect GROUP BY rfm_segment "
        "ORDER BY rate DESC"
    )
    assert checked.limit_added


# ------------------------------------------------------- the execution layer


def test_the_connection_refuses_to_serve_if_it_is_not_locked_down(runner):
    """
    SnapshotRunner proves its own controls at startup by attacking itself.

    This asserts the proof ran. If _assert_locked_down stopped refusing, the
    constructor raises and this fixture never yields.
    """
    assert runner.con is not None


def test_external_access_is_off_so_a_file_cannot_be_read(runner, secret):
    """
    The control that matters, and the one the guard is no longer alone in doing.

    Deliberately bypasses sql_guard and hands the statement straight to the
    engine: defence in depth means the second layer holds when the first is
    wrong, so testing it only through the first would prove nothing about it.
    """
    with pytest.raises(duckdb.Error, match="Permission Error") as excinfo:
        runner.con.execute(f"select * from read_csv('{secret.as_posix()}')")
    # Not merely that read_csv failed: it must fail on PERMISSIONS. An earlier
    # probe failed with "error when sniffing file", which is a format complaint
    # and would have been read as the control working while it was off.
    assert "Permission Error" in str(excinfo.value)


def test_external_access_is_off_so_a_file_cannot_be_written(runner, tmp_path):
    """
    The assertion is on the REASON, not merely that something was raised.

    `pytest.raises(Exception)` would pass on a typo in the statement, which is a
    test that reports safe without establishing anything -- the shape this
    project keeps finding. So each of these pins duckdb.Error and the refusal
    text, and the file's absence is checked separately: a refusal that wrote the
    file anyway is not a refusal.
    """
    target = (tmp_path / "exfil.csv").as_posix()
    with pytest.raises(duckdb.Error, match="Permission Error"):
        runner.con.execute(f"copy (select 1 as a) to '{target}'")
    assert not (tmp_path / "exfil.csv").exists(), "COPY was refused but the file was written"


def test_an_extension_cannot_be_installed(runner):
    with pytest.raises(duckdb.Error, match="Permission Error"):
        runner.con.execute("install httpfs")


def test_a_remote_database_cannot_be_attached(runner):
    # ATTACH over https needs httpfs, and installing it is already blocked, so
    # the refusal surfaces as the extension install failing. Matched on that
    # rather than on a type, because the path to "no" is what is being asserted.
    with pytest.raises(duckdb.Error, match="httpfs|Permission|extension"):
        runner.con.execute("attach 'https://example.com/x.db' as remote")


def test_the_configuration_cannot_be_unlocked_by_a_query(runner):
    """
    lock_configuration is what makes the line above it hold.

    Without it `set enable_external_access = true` is a single statement away,
    and every control below it becomes advisory.
    """
    for sql in (
        "set enable_external_access = true",
        "set lock_configuration = false",
    ):
        with pytest.raises(duckdb.Error, match="Cannot change configuration"):
            runner.con.execute(sql)


def test_the_connection_is_read_only(runner):
    with pytest.raises(duckdb.Error, match="read-only|Cannot execute"):
        runner.con.execute("create table pwn as select 1")


def test_a_pathological_query_is_interrupted_rather_than_hanging(runner):
    """
    One shared connection serves every visitor, so a slow query is not a private
    problem -- it is the demo being down for everyone else.
    """
    import time

    runner_timeout = runner.timeout_seconds
    started = time.monotonic()
    with pytest.raises(QueryTimeout, match="cancelled"):
        runner.run("select count(*) from range(100000000000) a, range(1000000) b")
    elapsed = time.monotonic() - started
    assert elapsed < runner_timeout + 5, f"interrupt took {elapsed:.1f}s"


def test_a_legitimate_query_still_returns_rows(runner):
    """The controls are worth nothing if they also stop the demo working."""
    checked = guarded(
        "SELECT rfm_segment, SUM(orders_mentioning_aspect) AS n "
        "FROM olist_marts.fct_segment_aspect GROUP BY rfm_segment"
    )
    frame = runner.run(checked.sql, max_rows=200)
    assert len(frame) > 0
    assert "rfm_segment" in frame.columns


def test_the_row_cap_is_applied_by_the_runner_not_only_by_the_guard(runner):
    frame = runner.run("SELECT * FROM fct_orders", max_rows=5)
    assert len(frame) == 5
