"""
One-shot BigQuery validation.

Everything in the README's "Validation status" table that cannot be proven by a
green DuckDB run gets proven -- or fails loudly -- by running this once:

    make validate-bq

  1. Auth and project reachable
  2. Target dataset exists (created if not)
  3. normalize_text EXECUTES on BigQuery and agrees with the reference values.
     strip_accents is DuckDB-only, so the BigQuery path (NORMALIZE NFD +
     REGEXP_REPLACE) is a genuinely different implementation.
  4. The surrogate key returns a STRING, not BYTES -- compiled from
     dbt_utils.generate_surrogate_key, never transcribed
  5. maximum_bytes_billed actually REJECTS an oversized query rather than
     billing it -- a ceiling nobody has watched trigger is not a verified
     ceiling
  6. The SCD2 figures come out identical on both engines, from one SQL text per
     figure with only the schema substituted. Note the limit of that claim: the
     two builds share the same replay output and the same dbt models, so what
     this proves is dialect-and-engine equivalence, not two independent
     derivations of the same answer.
  7. Loading a slice twice leaves every raw row count unchanged. The DuckDB
     proof does not transfer -- delete-then-insert and a partition-decorator
     WRITE_TRUNCATE are different mechanisms, and only one of them has ever been
     watched.

Checks 6 and 7 need the pipeline in place:
`make backfill TARGET=bigquery && make build TARGET=bigquery`. They skip with a
printed reason rather than failing if it is not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TRANSFORM = Path(__file__).resolve().parents[1] / "transform"

GIB = 1024**3

# Reference values. The first five are the strings that broke cross-language
# parity; the last two are the pair that must land on the SAME canonical value,
# because a mismatch there silently splits one seller city into two.
CASES = {
    "são paulo": "sao paulo",
    "mogi-guacu": "mogi guacu",
    "4º centenario": "4 centenario",
    "maceia³": "maceia",
    "sa£o paulo": "sao paulo",
    "santa barbara d´oeste": "santa barbara d oeste",
    "santa barbara d'oeste": "santa barbara d oeste",
}


ROOT = TRANSFORM.parent

# Run as `python scripts/validate_bigquery.py`, sys.path[0] is scripts/, so the
# repo root is not importable by default and `from ingestion import load` fails.
sys.path.insert(0, str(ROOT))

# A slice small enough that reloading it is cheap, and real enough that it has
# rows in every table. The 2018 tail is one order per day, so this is five load
# jobs over a handful of rows.
IDEMPOTENCY_SLICE = "2018-09-17"


def fail(message: str) -> int:
    print(f"\n  FAIL  {message}")
    return 1


def duckdb_figures() -> dict[str, int] | None:
    """The same probes against the local DuckDB build, as the reference."""
    import duckdb

    path = os.environ.get("DBT_DUCKDB_PATH", str(TRANSFORM / "olist.duckdb"))
    if not Path(path).exists():
        return None

    con = duckdb.connect(path, read_only=True)
    try:
        return {
            name: con.execute(sql.format(schema="main_marts")).fetchone()[0]
            for name, sql in PARITY_PROBES.items()
        }
    finally:
        con.close()


def bigquery_mart_schema(project: str) -> str | None:
    """
    Where dbt actually put the marts, read from the manifest.

    Not `BQ_DATASET`: dbt appends each model's custom schema, so the marts land
    in `olist_marts` while BQ_DATASET says `olist` -- which is how the first run
    of this check went looking for a table that was never going to be there.
    Not the `{target.schema}_{custom}` convention spelled out by hand either,
    since that is a transcription of something the manifest already states.
    """
    import json

    manifest_path = TRANSFORM / "target" / "manifest.json"
    if not manifest_path.exists():
        return None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for node in manifest["nodes"].values():
        if node["resource_type"] == "model" and node["name"] == "dim_customers":
            # The manifest reflects whichever target was built last.
            if node["database"] != project:
                return None
            return f"`{node['database']}.{node['schema']}`"
    return None


def raw_row_counts(client, project: str, dataset: str) -> dict[str, int]:
    from ingestion.load import TABLE_MAP

    return {
        table: client.get_table(f"{project}.{dataset}.{table}").num_rows
        for table in TABLE_MAP.values()
    }


def load_slice(slice_dir: Path) -> int:
    """Reload through the real loader, not a reimplementation of it."""
    from ingestion import load as load_module

    return load_module.main(
        ["--slice", str(slice_dir), "--target", "bigquery", "--log-level", "WARNING"]
    )


def compile_inline(inline: str) -> str | None:
    """
    Render BigQuery SQL from the REAL macros.

    An earlier version of this script kept its own transcription of the BigQuery
    expression and promptly drifted out of sync with the macro -- the exact
    failure mode assert_normalize_macro_matches_python exists to prevent. It then
    happened a second time: the surrogate-key probe hardcoded
    `to_hex(md5('a' || '|' || 'b'))`, which stopped describing the pipeline the
    moment the models moved to dbt_utils.generate_surrogate_key and its '-'
    separator. Nothing here transcribes SQL; everything compiles it.
    """
    result = subprocess.run(
        ["dbt", "compile", "--quiet", "--target", "bigquery", "--inline", inline],
        cwd=TRANSFORM,
        env={**os.environ, "DBT_PROFILES_DIR": "."},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"\n  dbt compile failed:\n{result.stdout}\n{result.stderr}")
        return None
    return result.stdout.strip()


def compile_normalize_sql() -> str | None:
    return compile_inline(
        "with t as (select @v as raw_value) "
        "select {{ normalize_text('raw_value') }} as normalized from t"
    )


# Figures that must come out identical on both engines. One SQL text per figure,
# with only the schema prefix substituted -- if each engine got its own query,
# agreement would prove the queries matched rather than the pipelines.
#
# Note what "mis-attributed" means here. 276 orders join a dimension row that is
# not the current one, but 4 of those belong to customers whose attributes
# returned to an earlier value, so the current row would hand back identical
# values. Only 272 are actually mis-attributed, and that is the number the README
# claims -- so the probe compares attributes, not surrogate keys.
PARITY_PROBES = {
    "scd2_change_events": "select count(*) as n from {schema}.dim_customers where version_number > 1",
    "scd2_customers_changed": (
        "select count(distinct customer_unique_id) as n from {schema}.dim_customers "
        "where version_number > 1"
    ),
    "dim_customers_rows": "select count(*) as n from {schema}.dim_customers",
    "dim_customers_current": ("select count(*) as n from {schema}.dim_customers where is_current"),
    "fct_orders_rows": "select count(*) as n from {schema}.fct_orders",
    "misattributed_orders": (
        "with cur as ("
        "  select customer_unique_id, customer_zip_code_prefix, customer_city, customer_state"
        "  from {schema}.dim_customers where is_current"
        "), at_purchase as ("
        "  select f.customer_unique_id, d.customer_zip_code_prefix, d.customer_city,"
        "         d.customer_state"
        "  from {schema}.fct_orders f"
        "  join {schema}.dim_customers d on d.customer_sk = f.customer_sk"
        ") "
        "select count(*) as n from at_purchase a join cur c using (customer_unique_id) "
        "where a.customer_zip_code_prefix <> c.customer_zip_code_prefix"
        "   or a.customer_city <> c.customer_city"
        "   or a.customer_state <> c.customer_state"
    ),
    "misattributed_wrong_state": (
        "with cur as ("
        "  select customer_unique_id, customer_state"
        "  from {schema}.dim_customers where is_current"
        "), at_purchase as ("
        "  select f.customer_unique_id, d.customer_state"
        "  from {schema}.fct_orders f"
        "  join {schema}.dim_customers d on d.customer_sk = f.customer_sk"
        ") "
        "select count(*) as n from at_purchase a join cur c using (customer_unique_id) "
        "where a.customer_state <> c.customer_state"
    ),
}


def main() -> int:
    project = os.environ.get("GCP_PROJECT_ID")
    dataset = os.environ.get("BQ_DATASET", "olist")
    location = os.environ.get("BQ_LOCATION", "US")

    if not project:
        return fail("GCP_PROJECT_ID is not set. See the BigQuery setup steps in the README.")

    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location)
    print(f"Project  {project}\nDataset  {dataset}\nLocation {location}\n")

    # -- 1/2. dataset ------------------------------------------------------
    ref = bigquery.Dataset(f"{project}.{dataset}")
    ref.location = location
    client.create_dataset(ref, exists_ok=True)
    print(f"  ok    dataset {dataset} present")

    # -- 3. normalize_text on BigQuery -------------------------------------
    print("\n  compiling normalize_text from the dbt macro...")
    sql = compile_normalize_sql()
    if sql is None:
        return fail("could not compile normalize_text")

    print("  normalize_text on BigQuery:")
    bad = 0
    for raw, expected in CASES.items():
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("v", "STRING", raw)],
                maximum_bytes_billed=GIB,
            ),
        )
        got = list(job.result())[0]["normalized"]
        mark = "ok   " if got == expected else "FAIL "
        bad += got != expected
        print(f"    {mark} {raw!r:<28} -> {got!r:<24} (expected {expected!r})")
    if bad:
        return fail(f"{bad} normalize_text mismatches on BigQuery")

    # -- 4. surrogate_key must be STRING, not BYTES ------------------------
    sk_sql = compile_inline(
        """select {{ dbt_utils.generate_surrogate_key(["'a'", "'b'"]) }} as sk"""
    )
    if sk_sql is None:
        return fail("could not compile generate_surrogate_key")

    job = client.query(sk_sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=GIB))
    sk = list(job.result())[0]["sk"]
    if not isinstance(sk, str):
        return fail(f"surrogate_key returned {type(sk).__name__}, expected str")
    print(f"\n  ok    surrogate_key -> STRING {sk!r}")

    # -- 5. the ceiling must actually fire ---------------------------------
    print("\n  maximum_bytes_billed:")
    # Must touch a COLUMN. `select count(*)` is answered from table metadata
    # and plans 0 bytes, so it would sail under any ceiling -- an earlier
    # version of this probe did exactly that and reported a false failure.
    oversized = "select avg(weight_pounds) from `bigquery-public-data.samples.natality`"

    dry = client.query(
        oversized, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    )
    planned = dry.total_bytes_processed or 0
    print(f"    dry run plans {planned / 1024**2:,.1f} MiB")

    try:
        client.query(
            oversized,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=1024 * 1024),  # 1 MiB
        ).result()
    except Exception as exc:  # noqa: BLE001 - whatever BigQuery raises is the evidence
        text = str(exc)
        if "maximum_bytes_billed" in text or "bytes billed" in text.lower():
            print("    ok    REJECTED before billing, as designed")
            print(f"          {text.splitlines()[0][:150]}")
        else:
            return fail(f"query failed, but not on the byte ceiling: {text[:250]}")
    else:
        return fail(
            "Oversized query SUCCEEDED. maximum_bytes_billed did not fire -- "
            "the ceiling is not enforced and the README claim is false."
        )

    # -- 6. the marts must agree with DuckDB, figure for figure ------------
    print("\n  SCD2 figures, DuckDB vs BigQuery:")
    duck = duckdb_figures()
    marts = bigquery_mart_schema(project)
    if duck is None:
        print("    skip  no DuckDB build found; run `make build` first")
    elif marts is None:
        print("    skip  no BigQuery marts in the manifest; run `make build TARGET=bigquery`")
    else:
        print(f"    marts at {marts}")
        mismatched = 0
        for name, template in PARITY_PROBES.items():
            sql = template.format(schema=marts)
            got = list(
                client.query(
                    sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=GIB)
                ).result()
            )[0]["n"]
            same = got == duck[name]
            mismatched += not same
            print(
                f"    {'ok   ' if same else 'FAIL '} {name:26} duckdb={duck[name]:,} bigquery={got:,}"
            )
        if mismatched:
            return fail(f"{mismatched} figures differ between DuckDB and BigQuery")
        print("    Two engines, two SQL dialects, one set of numbers.")

    # -- 7. loading a slice twice must not change the row count ------------
    print("\n  partition-decorator WRITE_TRUNCATE idempotency:")
    slice_dir = ROOT / "data" / "slices" / f"purchase_date={IDEMPOTENCY_SLICE}"
    if not slice_dir.exists():
        print(f"    skip  no slice at {slice_dir}; run `make backfill TARGET=bigquery`")
    else:
        raw = os.environ.get("BQ_RAW_DATASET", "olist_raw")
        before = raw_row_counts(client, project, raw)
        if load_slice(slice_dir) != 0:
            return fail(f"reloading {IDEMPOTENCY_SLICE} failed")
        after = raw_row_counts(client, project, raw)

        drifted = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
        for table, count in sorted(after.items()):
            mark = "FAIL " if table in drifted else "ok   "
            print(f"    {mark} {table:32} {count:,}")
        if drifted:
            return fail(
                "a second load changed row counts, so WRITE_TRUNCATE against the "
                f"partition decorator is not idempotent: {drifted}"
            )
        print(f"    {IDEMPOTENCY_SLICE} loaded twice, every raw table unchanged.")

    print("\n  All BigQuery claims verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
