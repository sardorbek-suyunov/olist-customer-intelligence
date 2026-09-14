"""
Natural language to SQL over the Olist marts, under both sets of ceilings.

    python -m analytics.nl2sql "which segment complains most about delivery?"

The interesting part of this is not the prompt. It is that every layer which
could cost money or touch data is a control that holds regardless of what the
model emits:

  gemini_budget   prices the call from countTokens BEFORE issuing it, reserves
                  it against a shared ledger, settles against real usage.
  sql_guard       parses the statement, rejects anything that is not a single
                  SELECT, and injects a LIMIT rather than asking for one.
  bq_safety       dry-runs the query for its exact byte count, checks it against
                  the per-query and per-session ceilings, and sets
                  maximum_bytes_billed so BigQuery enforces it server-side too.
  IAM             the service account holds roles/bigquery.dataViewer on the
                  marts dataset and nothing else. This is the control that
                  actually stops a DELETE; everything above is defence in depth.

TWO THINGS ARE GENERATED RATHER THAN WRITTEN
--------------------------------------------
The schema block comes from the dbt manifest, so a column added to a model
reaches the prompt without anyone remembering to update it -- and the
descriptions the agent reads are the same ones in _marts.yml that the docs site
renders. A hand-maintained schema string is a second copy of the schema, and
this project has already paid for several of those.

The worked examples come from `analytics.demo_examples`, which exist anyway as
the demo's zero-cost fallback. They are human-written and reviewed, which is
exactly what a few-shot example needs to be, and using them here means the
examples the agent learns from are the same ones a visitor can see run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from analytics import settings
from analytics.bq_safety import BudgetExceeded, QueryBudget, SafeQueryRunner
from analytics.demo_examples import EXAMPLES
from analytics.gemini_budget import DemoBudget, DemoBudgetExceeded
from analytics.sql_guard import UnsafeSQL, check

LOG = logging.getLogger("olist.nl2sql")

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "transform" / "target" / "manifest.json"
# The deployment fallback. `target/` is gitignored build output, so a clone --
# and Streamlit Cloud -- has no manifest. scripts/export_agent_schema.py freezes
# what the prompt needs into ~8 KB beside the demo's cached answers, for the same
# reason those are committed: the deployed demo must not depend on build output
# it cannot have.
FROZEN_SCHEMA = ROOT / "dashboard" / "data" / "agent_schema.json"

DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Only the marts. Not a security boundary -- see sql_guard -- but it keeps the
# prompt short and turns a wrong table into a clear message instead of a
# permission error the model cannot act on.
MARTS_SCHEMA = "olist_marts"


@dataclass(frozen=True)
class Answer:
    question: str
    sql: str
    rows: list[dict]
    bytes_billed: int
    usd_spent: float
    limit_added: bool
    tables: tuple[str, ...]


def schema_prompt(
    manifest_path: Path = MANIFEST,
    schema: str = MARTS_SCHEMA,
    columns_by_table: dict[str, list[str]] | None = None,
) -> str:
    """
    The marts, their columns and their descriptions.

    TWO SOURCES, BECAUSE NEITHER IS SUFFICIENT ALONE.

    The WAREHOUSE says which columns exist. The manifest does not: its `columns`
    dict contains only what someone wrote YAML for, and _marts.yml documents the
    columns that carry tests. Built from the manifest alone this prompt showed
    the agent 3 of fct_orders' 17 columns -- order_value, delivery_days and
    seller_count were all invisible -- and the agent duly invented a
    `fct_order_items` table to compute revenue from, four times. That reads like
    a hallucination and is closer to the opposite: it was never told the column
    it needed was right there.

    The MANIFEST says what the columns mean, and that is what stops the other
    kind of wrong answer. `aspect_rate_of_reviewed` and
    `aspect_rate_of_all_orders` have identical types and different meanings, and
    only the description separates them.

    So: column list from the database, descriptions overlaid from the manifest,
    and a column with no description is still listed rather than hidden.
    """
    if not manifest_path.exists():
        # A deployment has no manifest: `target/` is gitignored build output and
        # the deployed app has no warehouse to introspect either. Without this
        # fallback the live path fails on the first question with "run make
        # build" -- and ask.py catches that and tells the visitor no API key is
        # configured, which is the wrong cause and the wrong remedy, discoverable
        # only by typing a question into the deployed app.
        if FROZEN_SCHEMA.exists():
            return _prompt_from_frozen(FROZEN_SCHEMA, schema)
        raise SystemExit(
            f"{manifest_path} not found and no frozen schema at {FROZEN_SCHEMA}. "
            "Build the project (`make build`), or export the frozen schema with "
            "`python scripts/export_agent_schema.py`."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    lines: list[str] = []
    for node in manifest["nodes"].values():
        if node["resource_type"] != "model" or node["config"].get("schema") != "marts":
            continue
        name = node["name"]
        lines.append(f"\nTABLE {schema}.{name}")
        if node.get("description"):
            summary = " ".join(node["description"].split())
            lines.append(f"  -- {summary[:400]}")

        documented = {
            c["name"]: c.get("description") or "" for c in node.get("columns", {}).values()
        }
        actual = (columns_by_table or {}).get(name)
        for column in actual if actual else list(documented):
            note = " ".join(documented.get(column, "").split())
            lines.append(f"    {column}" + (f"  -- {note[:180]}" if note else ""))
    return "\n".join(lines)


def _prompt_from_frozen(path: Path, schema: str) -> str:
    """The same rendering, from the committed artifact instead of the manifest."""
    frozen = json.loads(path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for name, table in frozen["tables"].items():
        lines.append(f"\nTABLE {schema}.{name}")
        if table.get("description"):
            lines.append(f"  -- {table['description'][:400]}")
        for column in table["columns"]:
            note = column.get("description") or ""
            lines.append(f"    {column['name']}" + (f"  -- {note[:180]}" if note else ""))
    return "\n".join(lines)


def columns_from_bigquery(client, schema: str = MARTS_SCHEMA) -> dict[str, list[str]]:
    """Every column the marts actually have, in ordinal order."""
    rows = client.query(
        f"""select table_name, column_name
            from `{client.project}.{schema}.INFORMATION_SCHEMA.COLUMNS`
            order by table_name, ordinal_position"""
    ).result()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row.table_name, []).append(row.column_name)
    return out


def columns_from_snapshot(runner) -> dict[str, list[str]]:
    """
    Columns as the Parquet snapshot actually has them.

    Same principle as the BigQuery version: ask the thing that holds the data
    what columns it has, rather than the manifest, which only knows the ones
    somebody documented.
    """
    from analytics.snapshot_runner import SNAPSHOT_TABLES

    return {
        table: [row[0] for row in runner.con.execute(f"describe {table}").fetchall()]
        for table in SNAPSHOT_TABLES
    }


def mart_tables(manifest_path: Path = MANIFEST, schema: str = MARTS_SCHEMA) -> set[str]:
    """The marts that exist, from the same source the prompt is built from."""
    if not manifest_path.exists() and FROZEN_SCHEMA.exists():
        frozen = json.loads(FROZEN_SCHEMA.read_text(encoding="utf-8"))
        return {f"{schema}.{name}".lower() for name in frozen["tables"]}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        f"{schema}.{node['name']}".lower()
        for node in manifest["nodes"].values()
        if node["resource_type"] == "model" and node["config"].get("schema") == "marts"
    }


def example_prompt(schema: str = MARTS_SCHEMA) -> str:
    """Human-written, reviewed SQL. The demo's fallback, reused as few-shot."""
    blocks = []
    for example in EXAMPLES:
        sql = " ".join(example.sql.format(marts=schema).split())
        blocks.append(f"Q: {example.question}\nA: {sql}")
    return "\n\n".join(blocks)


def build_prompt(
    question: str,
    schema: str = MARTS_SCHEMA,
    columns_by_table: dict[str, list[str]] | None = None,
) -> str:
    return f"""You write BigQuery SQL against a small analytics warehouse.

{schema_prompt(schema=schema, columns_by_table=columns_by_table)}

RULES
- Return SQL only. No prose, no markdown fences, no explanation.
- One SELECT statement. Never INSERT, UPDATE, DELETE, CREATE or MERGE.
- Only the tables above, fully qualified as {schema}.<table>.
- Aggregate rather than returning raw rows where the question implies a summary.
- fct_segment_aspect has one row per (rfm_segment, aspect). Two consequences,
  and they pull in opposite directions:
  * A SEGMENT-level column (orders_in_segment, customers_in_segment,
    review_text_coverage_pct) is repeated once per aspect. Use DISTINCT or MAX
    to read it once. Do NOT SUM it -- that multiplies it by the aspect count.
  * An ASPECT-level measure (aspect_rate_of_reviewed, aspect_rate_of_all_orders,
    orders_mentioning_aspect) is per aspect. A question about a GROUP of aspects
    -- "delivery complaints", "product problems" -- means SUM that measure over
    the aspects in the group and GROUP BY rfm_segment. MAX would give the single
    worst aspect, which is a different question and a smaller number.
  * A question comparing segments returns ONE ROW PER SEGMENT. If your result
    has one row per (segment, aspect), you have not aggregated yet.
- aspect_rate_of_reviewed is the rate among orders that HAVE review text.
  aspect_rate_of_all_orders is over all orders. They are different questions and
  review_text_coverage_pct is the difference between them.

EXAMPLES

{example_prompt(schema=schema)}

Q: {question}
A:"""


def strip_fences(text: str) -> str:
    """
    Models wrap SQL in ```sql fences no matter how firmly they are told not to.

    Stripping it here rather than rejecting the answer: the instruction is a
    request to a non-deterministic system, and spending a retry -- and a slice
    of the visitor's budget -- to punish a formatting habit helps nobody.
    """
    fenced = re.search(r"```(?:sql)?\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


class Agent:
    """One conversation. Owns both budgets for its lifetime."""

    def __init__(
        self,
        bq_client=None,
        model: str = DEFAULT_MODEL,
        gemini_client=None,
        demo_budget: DemoBudget | None = None,
        query_budget: QueryBudget | None = None,
        max_rows: int | None = None,
        schema: str = MARTS_SCHEMA,
        runner=None,
    ) -> None:
        """
        Two execution backends, one agent.

        `bq_client` runs the SQL against BigQuery under bq_safety's byte
        ceilings -- what the gold set is scored against, because that is the
        deployed warehouse. `runner` takes any object with `.run(sql, max_rows)`,
        which is how the public demo executes against the committed Parquet
        snapshot instead: no credential on a public host, and no bytes billed by
        a stranger's question. See analytics/snapshot_runner.py.

        What does NOT change between them is everything that decides whether the
        SQL may run at all. The guard, the LIMIT and the Gemini budget are
        properties of the agent, not of the warehouse.
        """
        if bq_client is None and runner is None:
            raise ValueError("give the agent a bq_client or a runner")
        self.model = model
        self.schema = schema
        self.max_rows = max_rows if max_rows is not None else settings.nl2sql_max_rows()
        self.budget = demo_budget or DemoBudget(model=model, **settings.gemini_budget_settings())
        self.runner = runner or SafeQueryRunner(
            bq_client, query_budget or QueryBudget(**settings.query_budget_settings())
        )
        self._gemini = gemini_client
        self.tables = mart_tables(schema=schema)
        # The column list comes from the warehouse rather than the manifest,
        # which only knows the columns someone wrote YAML for. Against the
        # snapshot it comes from the Parquet files, for the same reason.
        self.columns = (
            columns_from_bigquery(bq_client, schema=schema)
            if bq_client is not None
            else columns_from_snapshot(self.runner)
        )

    def _client(self):
        if self._gemini is None:
            from enrichment.client import make_client

            self._gemini = make_client()
        return self._gemini

    def write_sql(self, question: str) -> tuple[str, float]:
        """Ask the model, paying for it through the ledger. Returns (sql, usd)."""
        from google.genai import types

        client = self._client()
        prompt = build_prompt(question, schema=self.schema, columns_by_table=self.columns)

        # Exact input tokens before the call, so the reservation is a real worst
        # case rather than a guess. countTokens is free.
        input_tokens = client.models.count_tokens(model=self.model, contents=prompt).total_tokens
        self.budget.check(input_tokens)

        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                # The output cap comes from the budget rather than being
                # restated here, so the reservation and the request cannot
                # disagree about what the ceiling is.
                **self.budget.generation_config(),
            ),
        )
        usage = response.usage_metadata
        spent = self.budget.record(
            usage.prompt_token_count or 0,
            (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
        )
        return strip_fences(response.text or ""), spent

    def ask(self, question: str) -> Answer:
        sql, spent = self.write_sql(question)
        # The table list goes to the guard so a hallucinated table is refused
        # with a message naming what IS available -- something the model can
        # act on -- rather than reaching BigQuery and coming back a 404.
        # Without it the agent invented olist_marts.fct_order_items twice and
        # the guard waved both through.
        checked = check(sql, allowed_tables=self.tables, max_rows=self.max_rows)
        result = self.runner.run(checked.sql, max_rows=self.max_rows)
        # SafeQueryRunner returns (rows, bytes); the snapshot runner returns a
        # frame and bills nothing. Normalised here rather than making the
        # snapshot pretend to have a byte count it does not have.
        if isinstance(result, tuple):
            rows, billed = result
        else:
            rows, billed = result.to_dict("records"), 0
        return Answer(
            question=question,
            sql=checked.sql,
            rows=rows,
            bytes_billed=billed,
            usd_spent=spent,
            limit_added=checked.limit_added,
            tables=checked.tables,
        )


def main(argv: list[str] | None = None) -> int:
    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)-8s %(name)s  %(message)s")

    agent = Agent(bigquery.Client(), model=args.model)
    try:
        answer = agent.ask(" ".join(args.question))
    except DemoBudgetExceeded as exc:
        print(f"Budget ({exc.scope}): {exc}")
        return 2
    except (UnsafeSQL, BudgetExceeded) as exc:
        print(f"Refused: {exc}")
        return 3

    print(f"\n{answer.sql}\n")
    for row in answer.rows[:20]:
        print("  ", row)
    print(
        f"\n{len(answer.rows)} rows | {answer.bytes_billed / 1024**2:.1f} MiB billed "
        f"| ${answer.usd_spent:.5f} of Gemini"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
