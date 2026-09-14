"""
Total API spend, from every ledger that holds a piece of it.

    python scripts/reconcile_spend.py

THE PROBLEM THIS EXISTS FOR
---------------------------
There are two ledgers and nothing summed them.

  enrichment_cost_log   labelling, the eval reference, the v2 pass, embeddings.
                        Lives in the warehouse and in the committed Parquet.
  demo_ledger.json      every NL->SQL call, from the demo and from the agent
                        eval. Lives on disk beside the dashboard, gitignored
                        because it is per-deployment runtime state.

The README quoted the first one. The agent's spend was therefore invisible to
the figure that describes what this project cost -- which is the same shape as
the two bypasses already in the failure table: not a wrong number, a number that
does not know about some of the events it claims to summarise.

AND NEITHER OF THEM CAN SEE THE BEGINNING
-----------------------------------------
The cost log's earliest row is the first full corpus run. The pilots before it
predate the log existing at all, so no amount of summing recovers them. That gap
is real, it is stated here rather than silently omitted, and its size comes from
a console reading recorded at the time: the console said $4.75 when the log said
$3.19.

That is a remembered number, not a measured one, and it is labelled as such. It
is the one figure in this project that cannot be regenerated, because the only
system that holds it is Google's billing console and there is no API for
"how much have I spent" without a BigQuery billing export configured in advance.
`--verify` prints the console URL to check it against.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
COST_LOG = ROOT / "enrichment" / "data" / "enrichment_cost_log.parquet"
DEMO_LEDGER = ROOT / "dashboard" / "data" / "demo_ledger.json"
OUT = ROOT / "enrichment" / "eval" / "spend.json"

# A console reading taken on 2026-09-13, when the cost log totalled $3.19.
# Remembered, not measured, and the only figure here that is. It exists because
# the earliest pilots ran before the cost log did.
CONSOLE_READING_USD = 4.75
COST_LOG_AT_THAT_READING_USD = 3.19

BUDGET_CEILING_USD = 7.39


def main(argv: list[str] | None = None) -> int:
    import duckdb

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify", action="store_true", help="print the console URL to check against"
    )
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    con = duckdb.connect()
    logged = float(
        con.execute(f"select sum(cost_usd) from '{COST_LOG.as_posix()}'").fetchone()[0] or 0.0
    )
    con.close()

    demo = 0.0
    demo_calls = 0
    if DEMO_LEDGER.exists():
        ledger = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
        demo = float(ledger.get("lifetime_usd", 0.0))
        demo_calls = int(ledger.get("calls", 0))

    pre_log = CONSOLE_READING_USD - COST_LOG_AT_THAT_READING_USD
    total = logged + demo + pre_log

    print(f"{'enrichment cost log':<34}${logged:>8.4f}   measured, per-call")
    print(f"{'demo ledger (NL->SQL + agent eval)':<34}${demo:>8.4f}   measured, {demo_calls} calls")
    print(f"{'pilots before the log existed':<34}${pre_log:>8.4f}   REMEMBERED from a console")
    print(f"{'':-<34}{'':->9}")
    print(f"{'estimated total':<34}${total:>8.4f}")
    print(f"{'ceiling':<34}${BUDGET_CEILING_USD:>8.2f}")
    print(f"{'remaining':<34}${BUDGET_CEILING_USD - total:>8.4f}")
    print()
    print(
        f"${logged + demo:.4f} of that is measured per call. ${pre_log:.2f} is a single\n"
        "remembered console figure and is the delta the README states rather than hides."
    )

    if args.verify:
        try:
            import google.auth
            import google.auth.transport.requests as tr
            import requests

            creds, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(tr.Request())
            response = requests.get(
                f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo",
                headers={"Authorization": f"Bearer {creds.token}"},
                timeout=30,
            )
            account = response.json().get("billingAccountName", "").split("/")[-1]
            print()
            print("Check against the console -- there is no API that returns spend:")
            print(f"  https://console.cloud.google.com/billing/{account}/reports")
            print("  Filter to the Generative Language API and the project's lifetime.")
        except Exception as exc:  # noqa: BLE001 - verification is a convenience
            print(f"\n(could not resolve the billing account: {str(exc)[:120]})")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "logged_usd": round(logged, 4),
                "demo_ledger_usd": round(demo, 4),
                "demo_ledger_calls": demo_calls,
                "pre_log_usd": round(pre_log, 4),
                "pre_log_provenance": "console reading 2026-09-13, not reproducible",
                "measured_usd": round(logged + demo, 4),
                "total_usd": round(total, 4),
                "ceiling_usd": BUDGET_CEILING_USD,
                "remaining_usd": round(BUDGET_CEILING_USD - total, 4),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
