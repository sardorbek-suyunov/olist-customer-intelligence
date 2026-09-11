"""
Export the built marts to Parquet for the public dashboard.

The Streamlit demo reads these files by default rather than querying BigQuery,
so the deployed dashboard keeps working when a service-account key expires,
the free tier is exhausted, or the GCP project is deleted. A portfolio link
that 500s six months after it was shared is worse than no link.

The snapshot is committed (see the .gitignore exception for
dashboard/data/*.parquet) and regenerated with `make snapshot`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "transform" / "olist.duckdb"
OUT = ROOT / "dashboard" / "data"

MARTS = ["dim_customers", "dim_products", "dim_sellers", "fct_orders"]


def main() -> int:
    if not WAREHOUSE.exists():
        print(f"No warehouse at {WAREHOUSE}. Run `make build` first.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE), read_only=True)

    manifest: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "duckdb marts built by `make build`",
        "tables": {},
    }

    for table in MARTS:
        target = OUT / f"{table}.parquet"
        con.execute(
            f"copy (select * from main_marts.{table}) to '{target.as_posix()}' "
            "(format parquet, compression zstd)"
        )
        rows = con.execute(f"select count(*) from main_marts.{table}").fetchone()[0]
        size_kb = target.stat().st_size / 1024
        manifest["tables"][table] = {"rows": rows, "bytes": target.stat().st_size}
        print(f"  {table:<16} {rows:>8,} rows  {size_kb:>8.1f} KiB")

    (OUT / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total = sum(t["bytes"] for t in manifest["tables"].values()) / 1024**2
    print(f"\nSnapshot total: {total:.1f} MiB -> {OUT}")
    if total > 45:
        print("WARNING: snapshot is large for a git repository. Consider sampling.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
