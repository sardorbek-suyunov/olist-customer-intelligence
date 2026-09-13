"""
Olist batch pipeline — two DAGs over one shared task factory.

SCHEDULE SHAPE
--------------
The extract covers 2016-09-04 .. 2018-10-17. Backfilling that at daily
granularity would be ~775 DAG runs to process ~120 MB: all scheduler overhead,
no engineering value. So:

    olist_backfill_monthly  @monthly  2016-09-01 .. 2018-09-01   25 runs
    olist_incremental_daily @daily    2018-09-17 ..  (open)      ~31 runs
                                                                 then no-ops

The monthly DAG does the bulk history; the daily DAG demonstrates the
incremental path on the tail of the data and is the one that keeps running on
a schedule. Together: ~56 runs instead of ~775.

POST-COVERAGE BEHAVIOUR (ADR 0002)
----------------------------------
Runs whose logical date is past 2018-10-17 short-circuit with a logged reason
and go green. No synthetic date mapping, no fabricated recency. The README
does not claim live daily operation.

THIN WRAPPER
------------
Every task shells out to a CLI-invokable module (`python -m ingestion.replay`,
`dbt build`). No business logic lives in this file, so swapping Airflow for
Dagster or cron means rewriting this module and nothing else. Each command
here can be run by hand from a terminal, which is also how they are debugged.
"""

from __future__ import annotations

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG, task

# --------------------------------------------------------------------------
# Coverage bounds. Kept in sync with ingestion/replay.py, which enforces them
# independently -- the DAG short-circuits early for legibility in the UI, the
# module re-checks so a manual CLI run behaves identically.
# --------------------------------------------------------------------------
DATA_COVERAGE_START = pendulum.datetime(2016, 9, 4, tz="UTC")
DATA_COVERAGE_END = pendulum.datetime(2018, 10, 17, tz="UTC")

# Where the monthly backfill hands over to the daily schedule.
DAILY_HANDOVER = pendulum.datetime(2018, 9, 17, tz="UTC")

PROJECT_ROOT = "{{ var.value.get('olist_project_root', '/opt/airflow/project') }}"
DBT_TARGET = "{{ var.value.get('olist_dbt_target', 'bigquery_scheduled') }}"

DEFAULT_ARGS = {
    "owner": "data-engineering",
    "retries": 2,
    "retry_delay": pendulum.duration(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": pendulum.duration(minutes=30),
    "depends_on_past": False,
}


@task.short_circuit
def within_coverage(data_interval_start=None, **_) -> bool:
    """
    Stop the run cleanly once the source extract is exhausted.

    Returning False marks downstream tasks skipped rather than failed, so the
    run is green and does not page. See ADR 0002 for why this is preferred
    over mapping wall-clock time onto a synthetic Olist date.
    """
    import logging

    log = logging.getLogger(__name__)

    if data_interval_start > DATA_COVERAGE_END:
        log.info(
            "Logical date %s is beyond the Olist coverage window (%s .. %s). "
            "Nothing to ingest. This is expected: the source is a static "
            "historical extract, not a live feed. See docs/adr/0002.",
            data_interval_start.to_date_string(),
            DATA_COVERAGE_START.to_date_string(),
            DATA_COVERAGE_END.to_date_string(),
        )
        return False

    log.info("Processing window starting %s", data_interval_start.to_date_string())
    return True


def build_pipeline(dag: DAG) -> None:
    """Attach the shared task chain to a DAG. Identical for both schedules."""
    with dag:
        start = EmptyOperator(task_id="start")

        gate = within_coverage()

        # Half-open [data_interval_start, data_interval_end) -- the same
        # interval convention as the SCD2 validity windows, so consecutive
        # runs partition the timeline exactly once.
        replay = BashOperator(
            task_id="replay_slice",
            bash_command=(
                f"cd {PROJECT_ROOT} && "
                "python -m ingestion.replay "
                "--start {{ data_interval_start | ds }} "
                "--end {{ data_interval_end | ds }} "
                "--out data/slices"
            ),
        )

        load = BashOperator(
            task_id="load_to_warehouse",
            bash_command=(
                f"cd {PROJECT_ROOT} && "
                "python -m ingestion.load "
                "--slice data/slices/purchase_date={{ data_interval_start | ds }} "
                f"--target {DBT_TARGET}"
            ),
        )

        # A load is five tables with no transaction spanning them, so a task
        # killed partway leaves the slice torn. The loader writes a completion
        # marker only once every table has landed; this asserts the marker is
        # there before dbt is allowed to build on the slice. Retrying the load
        # is the repair, which is why this sits between the two.
        verify = BashOperator(
            task_id="verify_slice_complete",
            bash_command=(
                f"cd {PROJECT_ROOT} && "
                "python -m ingestion.load "
                "--slice data/slices/purchase_date={{ data_interval_start | ds }} "
                f"--target {DBT_TARGET} "
                "--verify"
            ),
        )

        # `dbt build` runs models and tests interleaved, so a failing test
        # stops its downstream models rather than letting bad data propagate.
        transform = BashOperator(
            task_id="dbt_build",
            bash_command=(
                f"cd {PROJECT_ROOT}/transform && "
                "DBT_PROFILES_DIR=. dbt build "
                f"--target {DBT_TARGET} "
                '--vars \'{{"run_date": "{{ data_interval_start | ds }}"}}\''
            ),
        )

        finish = EmptyOperator(task_id="finish", trigger_rule="none_failed_min_one_success")

        start >> gate >> replay >> load >> verify >> transform >> finish


backfill_monthly = DAG(
    dag_id="olist_backfill_monthly",
    description="Monthly historical backfill, 2016-09 to 2018-09 (25 runs).",
    schedule="@monthly",
    start_date=pendulum.datetime(2016, 9, 1, tz="UTC"),
    end_date=DAILY_HANDOVER,
    catchup=True,
    # Bounded concurrency: the backfill is 25 runs and each one rebuilds the
    # marts, so unbounded parallelism would thrash the warehouse and make the
    # run order non-deterministic.
    max_active_runs=3,
    default_args=DEFAULT_ARGS,
    tags=["olist", "backfill", "monthly"],
    doc_md=__doc__,
)
build_pipeline(backfill_monthly)


incremental_daily = DAG(
    dag_id="olist_incremental_daily",
    description="Daily incremental on the tail of the extract; no-ops past 2018-10-17.",
    schedule="@daily",
    start_date=DAILY_HANDOVER,
    catchup=True,
    max_active_runs=4,
    default_args=DEFAULT_ARGS,
    tags=["olist", "incremental", "daily"],
    doc_md=__doc__,
)
build_pipeline(incremental_daily)
