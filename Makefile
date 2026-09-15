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
profile: ## Regenerate docs/data_profiling.md, docs/figures.json and the parity seed
	python scripts/profile_dataset.py

.PHONY: build
build: ## Run the full dbt build (models + tests). TARGET=duckdb|bigquery
	$(DBT) build --target $(TARGET)

.PHONY: dbt-test
dbt-test: ## Run dbt tests only
	$(DBT) test --target $(TARGET)

.PHONY: test
test: ## Run the Python unit tests
	python -m pytest analytics/tests ingestion/tests enrichment/tests -q

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

.PHONY: backfill
backfill: ## Replay + load the whole coverage window. TARGET=duckdb|bigquery
	python -m ingestion.backfill --target $(TARGET)

.PHONY: taxonomy-seed
taxonomy-seed: ## Regenerate the dbt aspect seed from enrichment/taxonomy.py
	python scripts/export_taxonomy_seed.py

.PHONY: taxonomy-seed-check
taxonomy-seed-check: ## Fail if the aspect seed has drifted from the taxonomy
	python scripts/export_taxonomy_seed.py --check

.PHONY: demo-examples
demo-examples: ## Recompute the public demo's cached answers from the marts
	python scripts/export_demo_examples.py --duckdb-path $(or $(DB),transform/olist.duckdb)

.PHONY: embedding-cost
embedding-cost: ## Measure what embedding the corpus would cost, before spending it
	python scripts/cost_embeddings.py

.PHONY: eval-score
eval-score: ## Score the shipped labels against the reference model
	python scripts/eval_score.py --duckdb-path $(or $(DB),transform/olist.duckdb) \
	  --sample enrichment/eval/eval_sample_600.json \
	  --subject $(or $(SUBJECT),gemini-3.1-flash-lite) \
	  --reference $(or $(REFERENCE),gemini-3.8-flash) \
	  --subject-version $(or $(SUBJECT_VERSION),v1) --reference-version v1

.PHONY: spend
spend: ## Total API spend across both ledgers, with the unmeasurable gap named
	python scripts/reconcile_spend.py --verify

.PHONY: dashboard-deploy-check
dashboard-deploy-check: ## Everything the deployed app needs, checked locally
	python -m pytest dashboard/tests -q
	python scripts/export_snapshot.py
	python scripts/export_demo_examples.py
	python scripts/export_agent_schema.py

.PHONY: secrets
secrets: ## Scan the FULL commit history for credentials (not just the tree)
	@echo 'Isolated on purpose: trufflehog3 pins attrs==20.3.0 and breaks dbt.'
	pipx run trufflehog3 --no-current --depth 10000 --config .trufflehog3.yml .

.PHONY: embed
embed: ## Embed the distinct review texts. Resumes from cache; a re-run costs $0
	python -m enrichment.embed --all --dimensions $(or $(DIMS),1536)

.PHONY: embed-reconcile
embed-reconcile: ## Check the cost log against the vectors that actually exist
	python scripts/reconcile_embedding_cost.py

.PHONY: readme
readme: ## Render README.md from README.template.md and the measured figures
	python scripts/render_readme.py

.PHONY: readme-check
readme-check: ## Fail if the committed README disagrees with the figures
	python scripts/render_readme.py --check

.PHONY: screenshot
screenshot: ## Regenerate the README's NL->SQL screenshot (needs `make dashboard` running; ~$0.0006)
	python scripts/capture_demo_screenshot.py

.PHONY: diagram
diagram: ## Render docs/img/architecture.svg from docs/architecture.mmd
	python scripts/render_diagram.py

.PHONY: docs
docs: ## Generate the dbt docs site and serve it at http://localhost:8080
	cd transform && DBT_PROFILES_DIR=. dbt docs generate --target duckdb
	cd transform && DBT_PROFILES_DIR=. dbt docs serve --port 8080

.PHONY: gemini-check
gemini-check: ## Resolve the Gemini model against your key, e.g. make gemini-check MODEL=<id>
	python scripts/check_gemini.py $(if $(MODEL),--model $(MODEL),)

.PHONY: enrich
enrich: ## Label review text. make enrich MODEL=<id> SAMPLE=2000 BATCH=20
	python -m enrichment.enrich --model $(MODEL) --batch-size $(or $(BATCH),20) $(if $(SAMPLE),--sample $(SAMPLE),--all) --target $(TARGET)

.PHONY: enrich-export
enrich-export: ## Snapshot the enrichment tables to enrichment/data/*.parquet
	python scripts/export_enrichment.py --duckdb-path $(or $(DB),transform/olist.duckdb)

.PHONY: enrich-restore
enrich-restore: ## Load the committed enrichment snapshot into a warehouse (free, no key)
	python scripts/export_enrichment.py --duckdb-path $(or $(DB),transform/olist.duckdb) --restore

.PHONY: dag-run
dag-run: ## Execute one DAG run in Docker, e.g. make dag-run WINDOW=2016-09-01
	cd orchestration/docker && WINDOW=$(WINDOW) DAG_ID=$(or $(DAG_ID),olist_backfill_monthly) docker compose run --rm dag-run

.PHONY: dag-ui
dag-ui: ## Airflow UI at localhost:8080 against the mounted project
	cd orchestration/docker && docker compose up ui

.PHONY: load-static
load-static: ## Load the non-sliced reference tables. TARGET=duckdb|bigquery
	python -m ingestion.load_static --target $(TARGET)

.PHONY: verify-slices
verify-slices: ## Check every window's completion marker. TARGET=duckdb|bigquery
	python -m ingestion.backfill --target $(TARGET) --verify

.PHONY: validate-bq
validate-bq: ## Prove the BigQuery claims: normalize_text executes, ceiling fires
	python scripts/validate_bigquery.py

.PHONY: docs
docs: ## Build and serve the dbt documentation site
	$(DBT) docs generate --target $(TARGET)
	$(DBT) docs serve

.PHONY: lint
lint: ## Lint Python
	ruff check analytics dashboard enrichment ingestion orchestration scripts
	ruff format --check analytics dashboard enrichment ingestion orchestration scripts

.PHONY: fmt
fmt: ## Auto-format Python
	ruff check --fix analytics dashboard enrichment ingestion orchestration scripts
	ruff format analytics dashboard enrichment ingestion orchestration scripts

.PHONY: clean
clean: ## Remove build artefacts
	rm -rf transform/target transform/dbt_packages transform/logs transform/*.duckdb data/slices

.PHONY: all
all: data profile build snapshot ## Full local run: raw CSVs -> tested marts -> dashboard snapshot
