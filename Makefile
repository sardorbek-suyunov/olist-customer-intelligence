.DEFAULT_GOAL := help
SHELL := /bin/bash

DBT := cd transform && DBT_PROFILES_DIR=. dbt
TARGET ?= duckdb

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install Python dependencies and dbt packages
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt
	$(DBT) deps

.PHONY: data
data: ## Download the Olist dataset from Kaggle into archive/
	python scripts/download_data.py

.PHONY: profile
profile: ## Regenerate docs/data_profiling.md and the normalization parity seed
	python scripts/profile_dataset.py

.PHONY: build
build: ## Run the full dbt build (models + tests). TARGET=duckdb|bigquery
	$(DBT) build --target $(TARGET)

.PHONY: dbt-test
dbt-test: ## Run dbt tests only
	$(DBT) test --target $(TARGET)

.PHONY: test
test: ## Run the Python unit tests
	python -m pytest analytics/tests ingestion/tests -q

.PHONY: test-dags
test-dags: ## Run DAG integrity tests (needs the Airflow environment)
	python -m pytest orchestration/tests -q

.PHONY: compile
compile: ## Compile without touching the warehouse (no data required)
	$(DBT) parse

.PHONY: snapshot
snapshot: ## Export the built marts to dashboard/data/*.parquet
	python scripts/export_snapshot.py

.PHONY: dashboard
dashboard: ## Launch the Streamlit dashboard against the committed snapshot
	streamlit run dashboard/app.py

.PHONY: replay
replay: ## Emit one slice, e.g. make replay START=2017-03-01 END=2017-04-01
	python -m ingestion.replay --start $(START) --end $(END)

.PHONY: docs
docs: ## Build and serve the dbt documentation site
	$(DBT) docs generate --target $(TARGET)
	$(DBT) docs serve

.PHONY: lint
lint: ## Lint Python
	ruff check analytics dashboard ingestion orchestration scripts
	ruff format --check analytics dashboard ingestion orchestration scripts

.PHONY: fmt
fmt: ## Auto-format Python
	ruff check --fix analytics dashboard ingestion orchestration scripts
	ruff format analytics dashboard ingestion orchestration scripts

.PHONY: clean
clean: ## Remove build artefacts
	rm -rf transform/target transform/dbt_packages transform/logs transform/*.duckdb data/slices

.PHONY: all
all: data profile build snapshot ## Full local run: raw CSVs -> tested marts -> dashboard snapshot
