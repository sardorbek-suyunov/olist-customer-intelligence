"""
Warehouse tables behind the enrichment: cache, map, quarantine, cost log.

CACHE KEY
---------
sha256(review_text + prompt_version + model). All three, because all three change
the answer. A prompt edit that did not invalidate the cache would serve
enrichments produced by a definition that no longer exists -- the same class of
bug as a README quoting a figure the data no longer supports.

The hash is computed in Python, once, and stored. SQL never recomputes it: a
second implementation of a hash is a second implementation, and those have not
gone well here. `review_enrichment_map` carries review_id -> content_hash so dbt
joins on a stored value rather than re-deriving one.

Deduplication falls out of the key. 40,950 reviews carry text but only 35,616 are
distinct, so 13% of the corpus is answered from cache on the very first pass,
before any re-run.

QUARANTINE
----------
A response that will not parse, or that parses into something the schema forbids,
lands in `enrichment_quarantine` with its reason and its raw text. It is never
dropped. Silent loss is the failure mode for this kind of pipeline, and the
ingestion side already learned that lesson the expensive way.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_SCHEMA = "olist_raw"

RESULTS = "review_enrichment"
MAP = "review_enrichment_map"
QUARANTINE = "enrichment_quarantine"
COST_LOG = "enrichment_cost_log"


def content_hash(text: str, prompt_version: str, model: str) -> str:
    """The one definition. Never reimplemented in SQL."""
    payload = f"{text}\x00{prompt_version}\x00{model}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Labelled:
    content_hash: str
    prompt_version: str
    model: str
    aspects: list[str]
    sentiment: str
    severity: int
    no_content: bool


@dataclass(frozen=True)
class Quarantined:
    content_hash: str
    prompt_version: str
    model: str
    reason: str
    raw_response: str


@dataclass(frozen=True)
class CostRow:
    run_id: str
    model: str
    prompt_version: str
    batch_size: int
    reviews: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    wall_seconds: float


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------
class DuckDBStore:
    """
    The credential-free path. CI and the eval harness run entirely on this, and
    with a populated cache the whole pipeline runs with no API key at all --
    which is what makes the enrichment reproducible in a fork.
    """

    def __init__(self, path: Path) -> None:
        import duckdb

        self.path = path
        self.con = duckdb.connect(str(path))
        self._create()

    def _create(self) -> None:
        c = self.con.execute
        c(f"create schema if not exists {RAW_SCHEMA}")
        c(
            f"""create table if not exists {RAW_SCHEMA}.{RESULTS} (
                   content_hash varchar primary key, prompt_version varchar,
                   model varchar, aspects varchar, sentiment varchar,
                   severity integer, no_content boolean, created_at timestamp)"""
        )
        c(
            f"""create table if not exists {RAW_SCHEMA}.{MAP} (
                   review_id varchar, content_hash varchar, primary key (review_id))"""
        )
        c(
            f"""create table if not exists {RAW_SCHEMA}.{QUARANTINE} (
                   content_hash varchar, prompt_version varchar, model varchar,
                   reason varchar, raw_response varchar, created_at timestamp)"""
        )
        c(
            f"""create table if not exists {RAW_SCHEMA}.{COST_LOG} (
                   run_id varchar, model varchar, prompt_version varchar,
                   batch_size integer, reviews integer, input_tokens bigint,
                   output_tokens bigint, cost_usd double, wall_seconds double,
                   created_at timestamp)"""
        )

    def cached_hashes(self, hashes: list[str]) -> set[str]:
        if not hashes:
            return set()
        placeholders = ",".join("?" * len(hashes))
        rows = self.con.execute(
            f"select content_hash from {RAW_SCHEMA}.{RESULTS} "
            f"where content_hash in ({placeholders})",
            hashes,
        ).fetchall()
        return {r[0] for r in rows}

    def write_results(self, rows: list[Labelled]) -> None:
        if not rows:
            return
        now = _now()
        self.con.executemany(
            f"insert or replace into {RAW_SCHEMA}.{RESULTS} values (?,?,?,?,?,?,?,?)",
            [
                (
                    r.content_hash,
                    r.prompt_version,
                    r.model,
                    json.dumps(r.aspects),
                    r.sentiment,
                    r.severity,
                    r.no_content,
                    now,
                )
                for r in rows
            ],
        )

    def write_map(self, pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            return
        self.con.executemany(f"insert or replace into {RAW_SCHEMA}.{MAP} values (?,?)", pairs)

    def write_quarantine(self, rows: list[Quarantined]) -> None:
        if not rows:
            return
        now = _now()
        self.con.executemany(
            f"insert into {RAW_SCHEMA}.{QUARANTINE} values (?,?,?,?,?,?)",
            [
                (r.content_hash, r.prompt_version, r.model, r.reason, r.raw_response, now)
                for r in rows
            ],
        )

    def write_cost(self, row: CostRow) -> None:
        self.con.execute(
            f"insert into {RAW_SCHEMA}.{COST_LOG} values (?,?,?,?,?,?,?,?,?,?)",
            [*asdict(row).values(), _now()],
        )

    def totals(self) -> dict:
        row = self.con.execute(
            f"""select coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),
                       coalesce(sum(cost_usd),0), coalesce(sum(reviews),0),
                       count(*)
                from {RAW_SCHEMA}.{COST_LOG}"""
        ).fetchone()
        return {
            "input_tokens": row[0],
            "output_tokens": row[1],
            "cost_usd": row[2],
            "reviews": row[3],
            "runs": row[4],
        }

    def close(self) -> None:
        self.con.close()


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------
class BigQueryStore:
    """Same tables, same contract, in the raw dataset the loader already guards."""

    def __init__(self, project: str, dataset: str) -> None:
        from google.cloud import bigquery

        self.project = project
        self.dataset = dataset
        self.client = bigquery.Client(project=project)
        self._create()

    def _fq(self, table: str) -> str:
        return f"`{self.project}.{self.dataset}.{table}`"

    def _create(self) -> None:
        from google.cloud import bigquery

        reference = bigquery.Dataset(f"{self.project}.{self.dataset}")
        reference.location = os.environ.get("BQ_LOCATION", "US")
        self.client.create_dataset(reference, exists_ok=True)

        for ddl in (
            f"""create table if not exists {self._fq(RESULTS)} (
                    content_hash string, prompt_version string, model string,
                    aspects string, sentiment string, severity int64,
                    no_content bool, created_at timestamp)""",
            f"""create table if not exists {self._fq(MAP)} (
                    review_id string, content_hash string)""",
            f"""create table if not exists {self._fq(QUARANTINE)} (
                    content_hash string, prompt_version string, model string,
                    reason string, raw_response string, created_at timestamp)""",
            f"""create table if not exists {self._fq(COST_LOG)} (
                    run_id string, model string, prompt_version string,
                    batch_size int64, reviews int64, input_tokens int64,
                    output_tokens int64, cost_usd float64, wall_seconds float64,
                    created_at timestamp)""",
        ):
            self.client.query(ddl).result()

    def cached_hashes(self, hashes: list[str]) -> set[str]:
        from google.cloud import bigquery

        if not hashes:
            return set()
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("h", "STRING", hashes)]
        )
        rows = self.client.query(
            f"select content_hash from {self._fq(RESULTS)} where content_hash in unnest(@h)",
            job_config=config,
        ).result()
        return {r.content_hash for r in rows}

    def _insert(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        errors = self.client.insert_rows_json(f"{self.project}.{self.dataset}.{table}", rows)
        if errors:
            raise RuntimeError(f"insert into {table} failed: {errors}")

    def write_results(self, rows: list[Labelled]) -> None:
        now = _now().isoformat()
        self._insert(
            RESULTS,
            [
                {
                    "content_hash": r.content_hash,
                    "prompt_version": r.prompt_version,
                    "model": r.model,
                    "aspects": json.dumps(r.aspects),
                    "sentiment": r.sentiment,
                    "severity": r.severity,
                    "no_content": r.no_content,
                    "created_at": now,
                }
                for r in rows
            ],
        )

    def write_map(self, pairs: list[tuple[str, str]]) -> None:
        self._insert(MAP, [{"review_id": rid, "content_hash": h} for rid, h in pairs])

    def write_quarantine(self, rows: list[Quarantined]) -> None:
        now = _now().isoformat()
        self._insert(
            QUARANTINE,
            [{**asdict(r), "created_at": now} for r in rows],
        )

    def write_cost(self, row: CostRow) -> None:
        self._insert(COST_LOG, [{**asdict(row), "created_at": _now().isoformat()}])

    def totals(self) -> dict:
        row = next(
            iter(
                self.client.query(
                    f"""select coalesce(sum(input_tokens),0) i,
                               coalesce(sum(output_tokens),0) o,
                               coalesce(sum(cost_usd),0) c,
                               coalesce(sum(reviews),0) r, count(*) n
                        from {self._fq(COST_LOG)}"""
                ).result()
            )
        )
        return {
            "input_tokens": row.i,
            "output_tokens": row.o,
            "cost_usd": row.c,
            "reviews": row.r,
            "runs": row.n,
        }

    def close(self) -> None:
        return None
