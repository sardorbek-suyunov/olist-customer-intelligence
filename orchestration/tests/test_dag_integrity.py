"""
DAG integrity tests.

These are the only thing standing between a typo in olist_batch.py and a
scheduler that silently stops picking the DAGs up, so they assert the
structural properties the schedule design depends on rather than just
"it imports".

Run with `pytest orchestration/tests` inside an environment that has Airflow
installed (see orchestration/requirements-airflow.txt). The main CI job skips
them; the dag-import job runs them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("airflow", reason="Airflow is only installed in the dag-import CI job")

from airflow.models import DagBag  # noqa: E402

EXPECTED_DAGS = {"olist_backfill_monthly", "olist_incremental_daily"}


@pytest.fixture(scope="module")
def dagbag() -> DagBag:
    return DagBag(dag_folder="orchestration/dags", include_examples=False)


def test_no_import_errors(dagbag: DagBag) -> None:
    assert not dagbag.import_errors, f"DAG import failures: {dagbag.import_errors}"


def test_expected_dags_present(dagbag: DagBag) -> None:
    assert set(dagbag.dag_ids) >= EXPECTED_DAGS


@pytest.mark.parametrize("dag_id", sorted(EXPECTED_DAGS))
def test_concurrency_is_bounded(dagbag: DagBag, dag_id: str) -> None:
    """
    Both DAGs backfill with catchup=True. Without a max_active_runs ceiling
    the scheduler would launch every pending run at once, thrash the
    warehouse, and make run ordering non-deterministic.
    """
    dag = dagbag.get_dag(dag_id)
    assert 1 <= dag.max_active_runs <= 4, (
        f"{dag_id} has max_active_runs={dag.max_active_runs}; catchup backfills must stay bounded"
    )


def test_backfill_is_monthly_and_bounded(dagbag: DagBag) -> None:
    """
    ~25 monthly runs, not ~775 daily ones. The end_date matters as much as the
    schedule: without it the monthly DAG would keep running forever alongside
    the daily one and double-process the tail.
    """
    dag = dagbag.get_dag("olist_backfill_monthly")
    # `schedule`, not `schedule_interval`: the latter was removed in Airflow 3 and
    # this assertion had been raising AttributeError unnoticed, because the job
    # that runs it never got past installing Airflow.
    assert dag.schedule == "@monthly"
    assert dag.end_date is not None, "backfill DAG must stop at the daily handover"


def test_daily_dag_starts_at_handover(dagbag: DagBag) -> None:
    """The two schedules must abut exactly -- no gap, no overlap."""
    monthly = dagbag.get_dag("olist_backfill_monthly")
    daily = dagbag.get_dag("olist_incremental_daily")
    assert daily.start_date == monthly.end_date


@pytest.mark.parametrize("dag_id", sorted(EXPECTED_DAGS))
def test_coverage_gate_precedes_ingestion(dagbag: DagBag, dag_id: str) -> None:
    """
    ADR 0002: runs past the end of the extract must short-circuit. If the gate
    ever stops being upstream of the replay task, post-coverage runs would
    start doing real work against an empty window.
    """
    dag = dagbag.get_dag(dag_id)
    replay = dag.get_task("replay_slice")
    assert "within_coverage" in {t.task_id for t in replay.upstream_list}


@pytest.mark.parametrize("dag_id", sorted(EXPECTED_DAGS))
def test_dbt_cannot_build_on_an_unverified_slice(dagbag: DagBag, dag_id: str) -> None:
    """
    A load is five tables with no transaction spanning them, so a task killed
    partway leaves the slice torn. If the completeness gate ever stops being
    upstream of dbt, the build would model a half-loaded slice and every test
    downstream would pass on it -- the failure is silent, which is why it is
    asserted structurally rather than trusted to review.
    """
    dag = dagbag.get_dag(dag_id)
    transform = dag.get_task("dbt_build")
    assert "verify_slice_complete" in {t.task_id for t in transform.upstream_list}


@pytest.mark.parametrize("dag_id", sorted(EXPECTED_DAGS))
def test_every_templated_field_renders(dagbag: DagBag, dag_id: str) -> None:
    """
    Templates must compile, not merely exist.

    Both bugs found on the DAGs' first real execution were invisible to every
    check above this one. `dbt_build` carried `{{"run_date": ...}}` -- doubled
    braces copied from f-string escaping into a string that is not an f-string
    -- so Jinja read it as a print statement and the task could never render.
    Import succeeded, structure was correct, and the task was unrunnable.

    This compiles each templated field through the DAG's own Jinja environment,
    which is the cheapest possible version of running it.
    """
    dag = dagbag.get_dag(dag_id)
    env = dag.get_template_env()

    for task in dag.tasks:
        for field in task.template_fields:
            value = getattr(task, field, None)
            if not isinstance(value, str):
                continue
            try:
                env.from_string(value)
            except Exception as exc:  # noqa: BLE001 - the exception is the evidence
                pytest.fail(f"{dag_id}.{task.task_id}.{field} does not compile: {exc}\n{value}")


@pytest.mark.parametrize("dag_id", sorted(EXPECTED_DAGS))
def test_window_end_is_derived_not_read_from_the_interval(dagbag: DagBag, dag_id: str) -> None:
    """
    The replay window must not depend on `data_interval_end`.

    A manually triggered run in Airflow 3 has no schedule-derived interval:
    start and end both collapse onto the logical date, which rendered
    `--start 2016-09-01 --end 2016-09-01` and replay rejected the zero-width
    window. Scheduled runs were fine, so nothing short of executing a task could
    have caught it.
    """
    dag = dagbag.get_dag(dag_id)
    command = dag.get_task("replay_slice").bash_command

    assert "data_interval_end" not in command, (
        "replay_slice reads data_interval_end, which collapses onto the logical "
        "date for a manual run and yields a zero-width window"
    )
    assert "--end" in command


@pytest.mark.parametrize("dag_id", sorted(EXPECTED_DAGS))
def test_tasks_have_retries(dagbag: DagBag, dag_id: str) -> None:
    dag = dagbag.get_dag(dag_id)
    for task in dag.tasks:
        assert task.retries >= 1, f"{dag_id}.{task.task_id} has no retries configured"
