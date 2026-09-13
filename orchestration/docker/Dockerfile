# Airflow image for running the DAGs locally, against DuckDB.
#
# The point of this image is that the orchestrator executes rather than merely
# imports. CI proves the DAGs parse and keep their structural invariants, which
# is the same level of confidence the BigQuery path had right up until it ran
# and produced six bugs, two of which no amount of compiling could have found.
#
# DuckDB target on purpose: no credentials, no spend, and the failure modes that
# belong to Airflow are not the ones that belong to BigQuery. Run it against
# bigquery_scheduled separately once this is green.
#
# STATUS: this image has NOT been built. Docker Desktop on the development
# machine crashes at startup on an unrelated orphaned socket, so the DAG run that
# proves the orchestrator works was done on WSL2 instead, against the same
# Airflow version, constraints file and DuckDB target. Treat the Dockerfile as
# unverified until someone builds it -- which is exactly the distinction the
# README's validation table is built around, and it would be dishonest to relax
# it here just because the file looks obviously correct.
FROM apache/airflow:3.0.2-python3.12

USER root
# git: dbt deps clones packages. build-essential: some wheels still compile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# Airflow's own pins first, under the official constraints file, so the ~600
# transitive dependencies match what Airflow was tested against.
COPY orchestration/requirements-airflow.txt /tmp/requirements-airflow.txt
RUN pip install --no-cache-dir -r /tmp/requirements-airflow.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.0.2/constraints-3.12.txt"

# Then the project's runtime. Deliberately not constrained: these are the
# versions the pipeline is tested against, and letting Airflow's constraints
# override them would mean the container runs different code than CI does.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONPATH=/opt/airflow/project \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/project/orchestration/dags
