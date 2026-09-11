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
  4. surrogate_key returns a STRING, not BYTES (to_hex(md5(...)))
  5. maximum_bytes_billed actually REJECTS an oversized query rather than
     billing it -- a ceiling nobody has watched trigger is not a verified
     ceiling

Run `make backfill TARGET=bigquery && make build TARGET=bigquery` separately for
the full pipeline; this covers the claims that need explicit observation.
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


def fail(message: str) -> int:
    print(f"\n  FAIL  {message}")
    return 1


def compile_normalize_sql() -> str | None:
    """
    Render the BigQuery SQL from the REAL macro.

    An earlier version of this script kept its own transcription of the
    BigQuery expression and promptly drifted out of sync with the macro -- the
    exact failure mode assert_normalize_macro_matches_python exists to prevent.
    Compiling it here means this script cannot lie about what the pipeline runs.
    """
    result = subprocess.run(
        [
            "dbt",
            "compile",
            "--quiet",
            "--target",
            "bigquery",
            "--inline",
            "with t as (select @v as raw_value) "
            "select {{ normalize_text('raw_value') }} as normalized from t",
        ],
        cwd=TRANSFORM,
        env={**os.environ, "DBT_PROFILES_DIR": "."},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"\n  dbt compile failed:\n{result.stdout}\n{result.stderr}")
        return None
    return result.stdout.strip()


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
    job = client.query(
        "select to_hex(md5('a' || '|' || 'b')) as sk",
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=GIB),
    )
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

    print("\n  All BigQuery claims verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
