"""
Freeze the agent's schema prompt into a small committed artifact.

    python scripts/export_agent_schema.py

The agent builds its prompt from transform/target/manifest.json plus the
warehouse's own column list. Neither reaches a deployment: `target/` is
gitignored (1.2 MB of build output, correctly excluded) and the deployed app has
no warehouse to introspect -- it reads committed Parquet.

So on Streamlit Cloud the live path would have failed on the first question with
"manifest not found; run make build", and `ask.py` would have caught it and told
the visitor no API key was configured. Wrong cause, wrong remedy, and invisible
until someone typed a question into the deployed app.

This writes what the prompt actually needs -- table names, column names, and the
descriptions from _marts.yml -- as ~10 KB of JSON beside the demo's cached
answers, for the same reason those are committed: the deployed demo must not
depend on build output it cannot have.

Generated, never hand-edited. `make dashboard-deploy-check` regenerates it and
CI diffs the result, so a column added to a model reaches the deployed agent's
prompt without anyone remembering.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dashboard" / "data" / "agent_schema.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--duckdb-path", type=Path, default=ROOT / "transform" / "olist.duckdb")
    args = parser.parse_args(argv)

    from analytics.nl2sql import MANIFEST, MARTS_SCHEMA

    if not MANIFEST.exists():
        raise SystemExit(f"{MANIFEST} not found — run `make build` first.")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    # Columns come from the built warehouse, not the manifest, for the reason
    # documented in nl2sql.schema_prompt: the manifest only carries the columns
    # someone wrote YAML for, and a prompt built from it showed the agent 3 of
    # fct_orders' 17.
    import duckdb

    con = duckdb.connect(str(args.duckdb_path), read_only=True)
    try:
        tables: dict[str, object] = {}
        for node in manifest["nodes"].values():
            if node["resource_type"] != "model" or node["config"].get("schema") != "marts":
                continue
            name = node["name"]
            documented = {
                c["name"]: (c.get("description") or "").strip()
                for c in node.get("columns", {}).values()
            }
            columns = [row[0] for row in con.execute(f"describe main_marts.{name}").fetchall()]
            tables[name] = {
                "description": " ".join((node.get("description") or "").split()),
                "columns": [
                    {"name": c, "description": " ".join(documented.get(c, "").split())}
                    for c in columns
                ],
            }
    finally:
        con.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"schema": MARTS_SCHEMA, "tables": tables}, indent=2), encoding="utf-8"
    )
    total = sum(len(t["columns"]) for t in tables.values())
    print(
        f"wrote {args.out.relative_to(ROOT)} — {len(tables)} tables, {total} columns, "
        f"{args.out.stat().st_size / 1024:.1f} KiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
