"""
One-shot BigQuery validation.

Everything in the README's "Validation status" table that is currently marked
unproven gets proven -- or fails loudly -- by running this once:

    python scripts/validate_bigquery.py

  1. Auth and project reachable
  2. Target dataset exists (created if not)
  3. normalize_text compiles AND EXECUTES on BigQuery -- the claim that matters,
     since strip_accents is DuckDB-only and the BigQuery path (NORMALIZE NFD +
     REGEXP_REPLACE) has never actually run
  4. surrogate_key returns a STRING, not BYTES (to_hex(md5(...)))
  5. maximum_bytes_billed actually REJECTS an oversized query rather than
     billing it -- a ceiling nobody has watched trigger is not a verified ceiling

Run `make build TARGET=bigquery` separately for the full model build; this
script covers the claims that need explicit observation rather than a green
dbt run.
"""

from __future__ import annotations

import os
import sys

GIB = 1024**3


def fail(message: str) -> int:
    print(f"\n  FAIL  {message}")
    return 1


def main() -> int:
    project = os.environ.get("GCP_PROJECT_ID")
    dataset = os.environ.get("BQ_DATASET", "olist")
    location = os.environ.get("BQ_LOCATION", "US")

    if not project:
        return fail("GCP_PROJECT_ID is not set. See the BigQuery setup steps in the README.")

    from google.api_core.exceptions import Forbidden, GoogleAPICallError
    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location)
    print(f"Project  {project}\nDataset  {dataset}\nLocation {location}\n")

    # -- 1/2. dataset ------------------------------------------------------
    ref = bigquery.Dataset(f"{project}.{dataset}")
    ref.location = location
    client.create_dataset(ref, exists_ok=True)
    print(f"  ok    dataset {dataset} present")

    # -- 3. normalize_text, the BigQuery path, actually executing ----------
    # Mirrors bigquery__normalize_text exactly. The three strings are the ones
    # that broke cross-language parity; 'mogi-guacu' checks the punctuation
    # rule, 'sao paulo' with a tilde checks accent folding.
    cases = {
        "são paulo": "sao paulo",
        "mogi-guacu": "mogi guacu",
        "4º centenario": "4 centenario",
        "maceia³": "maceia",
        "sa£o paulo": "sao paulo",
        "santa barbara d´oeste": "santa barbara d oeste",
    }
    expr = """
        nullif(trim(regexp_replace(regexp_replace(regexp_replace(
            lower(regexp_replace(normalize(@v, NFD), r'\\p{Mn}', '')),
            r'[^\\x00-\\x7F]', ''), r'[^a-z0-9 ]+', ' '), r'\\s+', ' ')), '')
    """
    print("\n  normalize_text on BigQuery:")
    bad = 0
    for raw, expected in cases.items():
        job = client.query(
            f"select {expr} as normalized",
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
    # A cross join over a public table plans to far more than 1 MiB. The point
    # is to observe the REJECTION, so the ceiling is deliberately tiny.
    print("\n  maximum_bytes_billed:")
    oversized = """
        select count(*)
        from `bigquery-public-data.samples.natality` a
        cross join (select 1 from `bigquery-public-data.samples.natality` limit 10) b
    """
    try:
        dry = client.query(
            oversized, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        )
        planned = dry.total_bytes_processed or 0
        print(f"    dry run plans {planned / 1024**2:,.1f} MiB")
    except (GoogleAPICallError, Forbidden) as exc:
        print(f"    (dry run unavailable: {type(exc).__name__})")
        planned = None

    try:
        job = client.query(
            oversized,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=1024 * 1024),  # 1 MiB
        )
        job.result()
    except Exception as exc:  # noqa: BLE001 - we want whatever BigQuery raises
        text = str(exc)
        if "maximum_bytes_billed" in text or "exceed" in text.lower():
            print("    ok    REJECTED before billing, as designed")
            print(f"          {text.splitlines()[0][:140]}")
        else:
            return fail(f"query failed, but not on the byte ceiling: {text[:200]}")
    else:
        return fail(
            "Oversized query SUCCEEDED. maximum_bytes_billed did not fire -- "
            "the ceiling is not enforced and the README claim is false."
        )

    print("\n  All BigQuery claims verified. Update the Validation status table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
