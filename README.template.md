<!--
  GENERATED FILE — edit README.template.md, not this one.

  Every number below is rendered from measured figures by scripts/render_readme.py:
  docs/figures.json (written by scripts/profile_dataset.py, from the source data)
  and the dbt manifest plus pytest collection (from the build). Regenerate with
  `make profile && make readme`. CI fails if this file is stale.
-->

# Olist Customer Intelligence

An end-to-end batch data platform over the [Brazilian E-Commerce Public Dataset
by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce): raw CSVs
→ time-sliced replay → warehouse → tested dimensional marts → dashboard.

[![CI](https://github.com/USER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/USER/REPO/actions/workflows/ci.yml)

**{{dbt_tests}} dbt tests · {{python_tests}} Python tests · full build in ~5s on DuckDB · $0 to run**

---

## The finding this project is built around

Olist is a heavily-used portfolio dataset, so the interesting work is not "load
the CSVs" — it is noticing that the obvious model is wrong.

`dim_customers` needs Type 2 history so orders attribute to the customer
attributes true *at purchase time*. The idiomatic answer is `dbt snapshot` on
`customer_id`. Profiling shows it cannot work:

| Table | Rows | Distinct key | Repeat observations |
|---|---:|---:|---:|
| `olist_customers_dataset` | {{grain_customers_rows}} | {{grain_customers_distinct}} (`customer_id`) | **{{grain_customers_repeats}}** |
| `olist_orders_dataset` | {{grain_orders_rows}} | {{grain_orders_distinct}} (`customer_id`) | **{{grain_orders_repeats}}** |
| `olist_products_dataset` | {{grain_products_rows}} | {{grain_products_distinct}} (`product_id`) | **{{grain_products_repeats}}** |
| `olist_sellers_dataset` | {{grain_sellers_rows}} | {{grain_sellers_distinct}} (`seller_id`) | **{{grain_sellers_repeats}}** |

`customer_id` is an **order-scoped surrogate** — Olist mints a fresh one per
order, strictly 1:1 with `order_id`. A snapshot keyed on it emits {{grain_orders_rows}} records
with one version each and zero change events: a history table structurally
incapable of recording history.

The dimension is therefore **derived** at `customer_unique_id` grain, ordering
each customer's observations by the `order_purchase_timestamp` of the order that
minted them. Result: **{{scd2_change_events}} real change events across {{scd2_customers_changed}} customers**
({{dim_customers_current}} current + {{dim_customers_historical}} historical = {{dim_customers_rows}} rows).

`fct_orders` joins to the version valid at purchase time:

```sql
inner join dim_customers d
    on  d.customer_unique_id = ck.customer_unique_id
    and o.order_purchase_timestamp >= d.valid_from
    and o.order_purchase_timestamp <  d.valid_to    -- half-open
```

> **A naive join to the current row mis-attributes {{misattributed_orders}} of {{orders_total}} orders
> ({{misattributed_pct}}%) across {{misattributed_customers}} customers — {{misattributed_wrong_state}} of them to the wrong _state_.**

Small, and completely silent: without the as-of join those orders move revenue
between regions in every geographic report. → [ADR 0001](docs/adr/0001-derive-scd2-customers.md)

`dim_products` and `dim_sellers` are **Type 1 by necessity** — one observation
per key means no snapshot strategy can ever yield a second version. Shipping
empty snapshot configs to look thorough would be worse than not having them.

---

## Architecture

```
archive/*.csv
    │
    ▼  ingestion/replay.py  ── replays the static extract as time-ordered,
    │                          referentially-consistent Parquet slices
    ▼
data/slices/purchase_date=YYYY-MM-DD/
    │
    ▼  dbt staging (views)      stg_customers · stg_orders · stg_order_items · …
    │
    ▼  dbt intermediate         int_customer_address_versions   ← SCD2 derivation
    │
    ▼  dbt marts (tables)       dim_customers (SCD2) · dim_products · dim_sellers
    │                           fct_orders  ← as-of join
    ▼
scripts/export_snapshot.py ──▶ dashboard/data/*.parquet ──▶ Streamlit
```

Orchestrated by two Airflow 3 DAGs (`orchestration/dags/olist_batch.py`), each a
thin `BashOperator` wrapper over CLI-invokable modules — so swapping Airflow for
Dagster or cron means rewriting one file and nothing else.

### One codebase, two warehouses

| Target | Used for | Why |
|---|---|---|
| **DuckDB** | local dev, CI | Reads the CSVs in place. No credentials, no spend, ~5s full build. |
| **BigQuery** | deployed warehouse | Partitioning, clustering, and a real cost story. |

Dialect differences are handled by `adapter.dispatch` (see
[`normalize_text.sql`](transform/macros/normalize_text.sql)) and dbt's
cross-database macros, not by forking models.

---

## Quickstart

```bash
make install          # deps + dbt packages
make data             # fetch the dataset from Kaggle into archive/
make build            # {{dbt_models}} models, {{dbt_tests}} tests, against DuckDB
make dashboard        # Streamlit against the committed snapshot
```

No Kaggle account? Download the dataset manually, unzip into `archive/`, and
`make data` will verify the row counts instead of downloading.

The raw CSVs (~123 MB, and `olist_geolocation_dataset.csv` alone is ~60 MB) are
gitignored — the repo stays small and does not redistribute a CC BY-NC-SA
dataset.

---

## Data quality

{{dbt_tests}} dbt tests run interleaved with the models (`dbt build`), so a failing test
stops its downstream models rather than letting bad data propagate. Beyond the
usual `unique`/`not_null`/`relationships`, six singular tests guard invariants
that the framework cannot:

| Test | Guards |
|---|---|
| `assert_fct_orders_grain_preserved` | The as-of join stays exactly 1:1. Catches **fan-out** (which a *closed* interval would cause on the {{tied_pairs}} tied timestamps) and **drop-out** (which an unfloored version-1 `valid_from` would cause for a backdated late arrival) in one assertion. |
| `assert_no_overlapping_customer_versions` | No customer has two versions valid at once. |
| `assert_no_mojibake_in_mart_text` | No non-ASCII character survives into a normalized mart column. |
| `assert_exactly_one_current_version_per_customer` | Exactly one `is_current` row per customer. |
| `assert_customer_versions_reconcile_to_orders` | Every order contributes to exactly one version observation. |
| `assert_normalize_macro_matches_python` | The SQL macro and the Python helper agree on all **{{parity_rows}}** distinct location strings. Executed on DuckDB; the BigQuery path is compile-checked only (see *Validation status*). |

That last one is the most useful test in the repo, and it earned its place
within a minute of being written.

### A test that found a real bug immediately

`normalize()` in Python and `normalize_text` in SQL implement the same
transformation twice. A docstring saying "keep these in sync" is not a control,
so the parity test re-derives every string through the macro and diffs. It found
three disagreements on its first run:

| raw | Python (NFKD) | SQL macro | cause |
|---|---|---|---|
| `4º centenario` | `4o centenario` | `4 centenario` | NFKD decomposes `º`→`o`; `strip_accents` does not |
| `maceia³` | `maceia3` | `maceia` | NFKD decomposes `³`→`3` |
| `sa£o paulo` | `sao paulo` | `sa o paulo` | Python *deletes* non-ASCII; macro *replaced* it with a space |

Python was using **NFKD** (compatibility decomposition) while DuckDB's
`strip_accents` is **NFD**-based — genuinely different algorithms, plus a
delete-vs-replace mismatch. Both now implement one algorithm stated once, and
the asymmetry is deliberate: non-ASCII is **deleted** (so mojibake `sa£o paulo`
merges with real `sao paulo`) while punctuation becomes a **space** (so
`mogi-guacu` → `mogi guacu`).

### Where normalization actually matters

Honest accounting, because the intuitive answer is wrong:

- **Customers: {{spurious_versions_removed}} spurious versions removed.** The source already ships
  lowercased, ASCII-folded and trimmed. Normalization here is a *guard*, not a
  cleaner — and it rewrites {{customer_punct_rows}} rows across {{customer_punct_cities}} legitimately punctuated city
  names (`santa barbara d'oeste`, `mogi-guacu`, `dias d'avila`). So the
  dimension **diffs on the normalized value and displays the raw one**.
- **Sellers: {{seller_cities_raw}} → {{seller_cities_normalized}} distinct cities.** *Here* it is load-bearing —
  `são paulo`/`sao  paulo`/`sao paulo` and
  `santa barbara d'oeste`/`d´oeste`/`d oeste` are genuine data-entry noise.

Every figure above is generated by [`scripts/profile_dataset.py`](scripts/profile_dataset.py)
into [`docs/data_profiling.md`](docs/data_profiling.md), and **CI fails if the
committed doc is stale** — the numbers in this README cannot drift into fiction.

### The three corrupt strings it did *not* fix

`maceia³` and `sa£o paulo` are double-encoded UTF-8 that was then
accent-stripped, leaving a bare Latin-1 continuation byte. They decode cleanly
— `0xC3` + the stray character gives `maceió` and `são paulo` — so normalizing
them to `maceia` and `sao paulo` is *consistently wrong* for the first one.

They are documented rather than repaired, because the blast radius was
measured, not assumed:

| Source column | Non-ASCII rows | Mojibake rows | distinct | Reaches a mart? |
|---|---:|---:|---:|---|
| `customers.customer_city` | {{non_ascii_customers_rows}} | **{{mojibake_customers_rows}}** | {{mojibake_customers_distinct}} | — |
| `sellers.seller_city` | {{non_ascii_sellers_rows}} | **{{mojibake_sellers_rows}}** | {{mojibake_sellers_distinct}} | yes, but nothing corrupt: both spellings are accents an NFD pass resolves, and `santa barbara d´oeste` folds onto the same canonical `santa barbara d oeste` as `d'oeste` |
| `geolocation.geolocation_city` | {{non_ascii_geolocation_rows}} | **{{mojibake_geolocation_rows}}** | {{mojibake_geolocation_distinct}} | **no** — `stg_geolocation` computes city in a CTE and discards it; the model is keyed on `zip_code_prefix` |

The two columns measure different things, and conflating them is what made an
earlier version of this table wrong. *Non-ASCII* counts every accented character,
including the 2,085 perfectly good spellings of `são paulo`. *Mojibake* counts
only what survives accent-stripping — a character no accent explains.

**City is never a `JOIN` key or a `GROUP BY` key in any model** — every join is
on `zip_code_prefix` or an id. So nothing splits one city into two rows, and the
{{mojibake_rows_total}} corrupt values across {{source_rows_total}} source rows affect no
aggregate. Writing a byte-level repair into hot-path SQL to fix
{{mojibake_geolocation_distinct}} values nothing reads would be the wrong trade.

`assert_no_mojibake_in_mart_text` stops that reasoning from silently expiring:
if a non-ASCII character ever reaches a normalized mart column, the build fails
and the decision gets revisited against real numbers instead of a stale comment.

---

## Validation status

Honest accounting of what has actually been executed, versus what is
compile-checked only.

| | DuckDB | BigQuery |
|---|---|---|
| {{dbt_models}} models build | ✅ executed | ⬜ not yet run |
| {{dbt_tests}} dbt tests pass | ✅ executed | ⬜ not yet run |
| Normalization parity over {{parity_rows}} strings | ✅ executed | ⬜ not yet run |
| Macros render (`dbt parse`) | ✅ | ✅ |
| `maximum_bytes_billed` rejects an oversized query | n/a | ⬜ **not yet observed firing** |

Three dialect bugs were found and fixed by a static sweep for DuckDB-native
SQL, and all three would have failed at runtime on BigQuery:

1. **`md5()` returns `BYTES` on BigQuery, hex `VARCHAR` on DuckDB.** Undispatched,
   `customer_sk` would silently become a `BYTES` column and any join against a
   `STRING` key would fail at runtime. Now `to_hex(md5(...))` on BigQuery.
2. **`regexp_matches` is DuckDB-only** (BigQuery: `regexp_contains`).
3. **`dim_customers` could not be partitioned at all.** BigQuery time-unit
   partitioning only accepts values in 1960-01-01 … 2159-12-31, and version 1's
   `valid_from` is floored to 1900-01-01 — which would have put {{dim_customers_current}} of
   {{dim_customers_rows}} rows outside the legal range. Partitioning removed (it was also the
   wrong tool for a 4.5 MB dimension); clustering retained. `fct_orders` moved
   from daily to **monthly** partitions for the same sizing reason — 775
   partitions averaging 13 KB is far below the ~1 GB partition BigQuery is
   built around. Why partitioning was dropped rather than the sentinel moved:
   → [ADR 0003](docs/adr/0003-dim-customers-clustered-not-partitioned.md)

`strip_accents` was already adapter-dispatched, and `IS DISTINCT FROM` was
rewritten longhand so the parity test compiles identically everywhere.

**Until the BigQuery build has actually run, treat that column as unproven.**
A ceiling nobody has watched trigger is not a verified ceiling, and a macro
that has only ever compiled is not a tested macro.

---

## Schedule

The extract covers **2016-09-04 → 2018-10-17**. Backfilling that daily would be
~775 DAG runs to process 120 MB, so:

| DAG | Schedule | Window | Runs | `max_active_runs` |
|---|---|---|---:|---:|
| `olist_backfill_monthly` | `@monthly` | 2016-09 → 2018-09 | 25 | 3 |
| `olist_incremental_daily` | `@daily` | 2018-09-17 → open | ~31 | 4 |

**What happens after the data runs out.** Scheduled runs past 2018-10-17
short-circuit with a logged reason and go green:

```
Logical date 2026-09-11 is beyond the Olist coverage window
(2016-09-04 .. 2018-10-17). Nothing to ingest. This is expected:
the source is a static historical extract, not a live feed.
```

There is **no synthetic date mapping** and this project does not claim live
daily operation. Mapping wall-clock time onto a fake Olist date would make every
recency figure on the dashboard fabricated, and the cost of being caught at that
is much higher than the cost of an absent badge. → [ADR 0002](docs/adr/0002-post-2018-schedule.md)

---

## Cost controls

Everything here runs inside free tiers, but the ceilings are enforced by the
platform rather than by good intentions.

| Control | Where | Value |
|---|---|---|
| `maximum_bytes_billed` on every dbt job | [`profiles.yml`](transform/profiles.yml) | 2 GiB |
| Per-query ceiling for the NL→SQL agent | [`analytics/bq_safety.py`](analytics/bq_safety.py) | 1 GiB |
| Per-session cumulative budget | same | 5 GiB / 50 queries |
| Dry-run before every agent query | same | plan priced before it runs |
| Read-only statement guard | same | single `SELECT`/`WITH`, IAM-backed |

The whole warehouse is ~120 MB, so a legitimate query scans single-digit MB. A
runaway `select *` is rejected by BigQuery server-side before it bills anything,
and the per-session budget catches the death-by-a-thousand-queries case that a
per-query cap cannot see.

An LLM that writes SQL will eventually write a cross join. A prompt saying
"always add a LIMIT" is a suggestion to a non-deterministic system; a
`maximum_bytes_billed` on the job is a control.

---

## Repository layout

```
analytics/          NL->SQL spend ceilings and read-only guards (+ {{analytics_tests}} unit tests)
dashboard/          Streamlit app + committed Parquet snapshot of the marts
docs/adr/           Architecture decision records
docs/               Generated data profiling report
ingestion/          replay.py -- time-sliced, referentially-consistent extract
orchestration/      Airflow 3 DAGs + DAG integrity tests
scripts/            download_data, profile_dataset, export_snapshot
transform/          dbt project: staging -> intermediate -> marts
```

The dashboard reads the **committed Parquet snapshot** by default, with a
sidebar toggle for live BigQuery. A public demo that 500s six months after it
was shared because a service-account key expired is worse than no demo.

---

## Status

| Component | State |
|---|---|
| Replay harness, staging, SCD2 marts, {{dbt_tests}} dbt tests | **Built and passing** |
| DuckDB + BigQuery dual targets, cost ceilings | **Built** |
| Streamlit dashboard + committed snapshot | **Built** |
| Airflow DAGs + integrity tests | **Built**, import-verified in CI |
| `ingestion/load.py` (slice → warehouse) | **Built** — idempotent delete-then-insert (DuckDB) / partition-decorator `WRITE_TRUNCATE` (BigQuery) |
| dbt sources reading the *loaded* raw tables | Not wired — the DuckDB target still reads the CSVs in place for fast, credential-free CI |
| Executed BigQuery run | **Blocked** — needs a GCP project (see *Validation status*) |
| Gemini review enrichment + labelled eval set | Planned |
| NL→SQL agent (ceilings already built) | Planned |

Planned means planned. Nothing in this README describes code that does not
exist.

---

## Dataset licence

Brazilian E-Commerce Public Dataset by Olist, CC BY-NC-SA 4.0. Not
redistributed here — `make data` fetches it from Kaggle.
