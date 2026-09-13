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

[![CI](https://github.com/sardorbek-suyunov/olist-customer-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/sardorbek-suyunov/olist-customer-intelligence/actions/workflows/ci.yml)

**55 dbt tests · 48 Python tests · full build in ~5s on DuckDB · $0 to run**

---

## The finding this project is built around

Olist is a heavily-used portfolio dataset, so the interesting work is not "load
the CSVs" — it is noticing that the obvious model is wrong.

`dim_customers` needs Type 2 history so orders attribute to the customer
attributes true *at purchase time*. The idiomatic answer is `dbt snapshot` on
`customer_id`. Profiling shows it cannot work:

| Table | Rows | Distinct key | Repeat observations |
|---|---:|---:|---:|
| `olist_customers_dataset` | 99,441 | 99,441 (`customer_id`) | **0** |
| `olist_orders_dataset` | 99,441 | 99,441 (`customer_id`) | **0** |
| `olist_products_dataset` | 32,951 | 32,951 (`product_id`) | **0** |
| `olist_sellers_dataset` | 3,095 | 3,095 (`seller_id`) | **0** |

`customer_id` is an **order-scoped surrogate** — Olist mints a fresh one per
order, strictly 1:1 with `order_id`. A snapshot keyed on it emits 99,441 records
with one version each and zero change events: a history table structurally
incapable of recording history.

The dimension is therefore **derived** at `customer_unique_id` grain, ordering
each customer's observations by the `order_purchase_timestamp` of the order that
minted them. Result: **259 real change events across 252 customers**
(96,096 current + 259 historical = 96,355 rows).

`fct_orders` joins to the version valid at purchase time:

```sql
inner join dim_customers d
    on  d.customer_unique_id = ck.customer_unique_id
    and o.order_purchase_timestamp >= d.valid_from
    and o.order_purchase_timestamp <  d.valid_to    -- half-open
```

> **276 of 99,441 orders belong to a superseded
> version. 272 of those (0.274%, across
> 252 customers) receive materially different attributes from a
> naive join to the current row — 43 of them the wrong _state_.**

The other 4 belong to customers who moved away and
came back, so the current row happens to be correct for them. Worth separating: the
as-of join is load-bearing for 276 orders, but only
272 of them would actually be wrong without it.

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
make build            # 12 models, 55 tests, against DuckDB
make dashboard        # Streamlit against the committed snapshot
```

No Kaggle account? Download the dataset manually, unzip into `archive/`, and
`make data` will verify the row counts instead of downloading.

### What is committed, and what is not

| | Committed? | Why |
|---|---|---|
| `archive/*.csv` — the raw extract, ~123 MB | No | `make data` fetches it. Redistributing a CC BY-NC-SA dataset is not ours to do, and `olist_geolocation_dataset.csv` alone is ~60 MB. |
| `data/slices/**` — replay output, ~29 MB over 56 windows | No | Derived and regenerable with `make backfill`. Committing generated Parquet would contradict the clone-and-run claim. |
| `ingestion/tests/fixtures/purchase_date=2016-09-01` — 45 KB | **Yes** | One slice, so the loader tests run without a 123 MB download. Every table populated, and payments (3) do not match orders (4), so it exercises the child tables not being 1:1. |
| `dashboard/data/*.parquet` — the marts snapshot, ~15 MB | **Yes, deliberately** | This one is the exception that looks like the rule it breaks. A public demo that 500s six months after it was shared because a service-account key expired is worse than no demo, so the dashboard reads committed Parquet by default and treats live BigQuery as the opt-in. |

The distinction is between *derived data that regenerates in seconds* and *the
one artifact whose whole job is to survive the credentials going stale*.

---

## Data quality

55 dbt tests run interleaved with the models (`dbt build`), so a failing test
stops its downstream models rather than letting bad data propagate. Beyond the
usual `unique`/`not_null`/`relationships`, six singular tests guard invariants
that the framework cannot:

| Test | Guards |
|---|---|
| `assert_fct_orders_grain_preserved` | The as-of join stays exactly 1:1. Catches **fan-out** (which a *closed* interval would cause on the 290 tied timestamps) and **drop-out** (which an unfloored version-1 `valid_from` would cause for a backdated late arrival) in one assertion. |
| `assert_no_overlapping_customer_versions` | No customer has two versions valid at once. |
| `assert_no_mojibake_in_mart_text` | No non-ASCII character survives into a normalized mart column. |
| `assert_exactly_one_current_version_per_customer` | Exactly one `is_current` row per customer. |
| `assert_customer_versions_reconcile_to_orders` | Every order contributes to exactly one version observation. |
| `assert_normalize_macro_matches_python` | The SQL macro and the Python helper agree on all **12,818** distinct location strings. Executed on DuckDB; the BigQuery path is compile-checked only (see *Validation status*). |

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

- **Customers: 0 spurious versions removed.** The source already ships
  lowercased, ASCII-folded and trimmed. Normalization here is a *guard*, not a
  cleaner — and it rewrites 450 rows across 52 legitimately punctuated city
  names (`santa barbara d'oeste`, `mogi-guacu`, `dias d'avila`). So the
  dimension **diffs on the normalized value and displays the raw one**.
- **Sellers: 611 → 603 distinct cities.** *Here* it is load-bearing —
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
| `customers.customer_city` | 0 | **0** | 0 | — |
| `sellers.seller_city` | 3 | **0** | 0 | yes, but nothing corrupt: both spellings are accents an NFD pass resolves, and `santa barbara d´oeste` folds onto the same canonical `santa barbara d oeste` as `d'oeste` |
| `geolocation.geolocation_city` | 73,442 | **4** | 3 | **no** — `stg_geolocation` computes city in a CTE and discards it; the model is keyed on `zip_code_prefix` |

The two columns measure different things, and conflating them is what made an
earlier version of this table wrong. *Non-ASCII* counts every accented character,
including the 2,085 perfectly good spellings of `são paulo`. *Mojibake* counts
only what survives accent-stripping — a character no accent explains.

**City is never a `JOIN` key or a `GROUP BY` key in any model** — every join is
on `zip_code_prefix` or an id. So nothing splits one city into two rows, and the
4 corrupt values across 1,550,851 source rows affect no
aggregate. Writing a byte-level repair into hot-path SQL to fix
3 values nothing reads would be the wrong trade.

`assert_no_mojibake_in_mart_text` stops that reasoning from silently expiring:
if a non-ASCII character ever reaches a normalized mart column, the build fails
and the decision gets revisited against real numbers instead of a stale comment.

---

## Validation status

Honest accounting of what has actually been executed. Both columns have now run;
nothing below is inferred from a compile.

| | DuckDB | BigQuery |
|---|---|---|
| 12 models build | ✅ executed | ✅ executed |
| 55 dbt tests pass | ✅ executed | ✅ executed |
| Normalization parity over 12,818 strings | ✅ executed | ✅ executed |
| Raw backfill reconciles to source | ✅ executed | ✅ executed |
| Slice reload leaves row counts unchanged | ✅ executed | ✅ executed |
| `maximum_bytes_billed` rejects an oversized query | n/a | ✅ **observed firing** |

The SCD2 figures come out identical on both engines: 259 change events
across 252 customers, 96,096 current +
259 historical = 96,355 rows,
272 mis-attributed orders and 43 of them to
the wrong state. Checked by `make validate-bq`, which runs one SQL text per figure
against both warehouses with only the schema substituted — if each engine got its
own query, agreement would prove the queries matched rather than the pipelines.

**What that does and does not prove.** It is dialect-and-engine equivalence, not
two independent derivations of the same answer: both builds read the same replay
output through the same dbt models. A shared modelling error would reproduce
faithfully on both. What it rules out is the large class of bugs where one
dialect quietly means something different from the other — which, as below, is
not a hypothetical class.

The ceiling was watched rather than assumed. A query planned at 1,049.9 MiB was
rejected with `bytesBilledLimitExceeded` before billing anything. Note the exact
ceiling: that probe sets a deliberately low **1 MiB** limit to force the
rejection, not the **2 GiB** `maximum_bytes_billed` that
[`profiles.yml`](transform/profiles.yml) puts on every real job. What is proven
is that the mechanism rejects rather than bills; the production ceiling is the
same mechanism at a different number.

### The bugs this actually caught

Three were found by a static sweep for DuckDB-native SQL, before any BigQuery
run:

1. **`md5()` returns `BYTES` on BigQuery, hex `VARCHAR` on DuckDB.** Undispatched,
   `customer_sk` would silently become a `BYTES` column and any join against a
   `STRING` key would fail at runtime. Since resolved by deleting the local macro
   entirely — `dbt_utils.generate_surrogate_key` builds on the adapter's own
   `to_hex(md5(...))`, so there is no second implementation left to get wrong.
2. **`regexp_matches` is DuckDB-only** (BigQuery: `regexp_contains`).
3. **`dim_customers` could not be partitioned at all.** BigQuery time-unit
   partitioning only accepts values in 1960-01-01 … 2159-12-31, and version 1's
   `valid_from` is floored to 1900-01-01 — which would have put 96,096 of
   96,355 rows outside the legal range. Partitioning removed (it was also the
   wrong tool for a 4.5 MB dimension); clustering retained. `fct_orders` moved
   from daily to **monthly** partitions for the same sizing reason — 775
   partitions averaging 13 KB is far below the ~1 GB partition BigQuery is
   built around. Why partitioning was dropped rather than the sentinel moved:
   → [ADR 0003](docs/adr/0003-dim-customers-clustered-not-partitioned.md)

Three more needed a real run, and no amount of compiling would have found them:

4. **The partition column was not in the file.** The loader asked BigQuery to
   partition on `_slice_date` while `replay.py` was not writing that column, so
   the very first load job failed with *"The field specified for partitioning
   cannot be found in the schema."* Both halves were individually valid; they
   only disagreed when a real job read a real file.
5. **The parity seed declared `varchar`.** DuckDB's spelling for the type.
   BigQuery rejects it outright — *"Invalid value for type: VARCHAR is not a
   valid value"* — failing the seed and the eight models downstream of it.
6. **A reference table loaded with no column names.**
   `product_category_name_translation` landed as `string_field_0` /
   `string_field_1`, and `stg_products` failed with *"Name
   product_category_name not found inside t"*. BigQuery's CSV autodetect infers
   a header by finding a first row whose types differ from the body, which
   cannot work for a table that is strings all the way down. The three sibling
   tables loaded correctly, which is exactly what hid it: the same code, right
   three times and wrong once, with nothing failing until a join went looking
   for a column by name.

A fourth class showed up in ingestion rather than SQL, and is written up in
[the incident note](docs/incidents/2026-09-11-torn-backfill.md): an interrupted
backfill left a slice half-loaded and reported success, because the
reconciliation guarded `table in counts` and a table that was never reached
contributes no count to disagree with.

`strip_accents` was already adapter-dispatched, and `IS DISTINCT FROM` was
rewritten longhand so the parity test compiles identically everywhere.

**Six dialect bugs, three of which only a real run could find.** That ratio is
the argument for running it, and against a status table that says "compile-checked"
and means "works".

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
analytics/          NL->SQL spend ceilings and read-only guards (+ 19 unit tests)
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
| Replay harness, staging, SCD2 marts, 55 dbt tests | **Built and passing** |
| DuckDB + BigQuery dual targets, cost ceilings | **Built** |
| Streamlit dashboard + committed snapshot | **Built** |
| Airflow DAGs + integrity tests | **Built**, import-verified in CI |
| `ingestion/load.py` (slice → warehouse) | **Built and proven idempotent on both** — delete-then-insert (DuckDB) / partition-decorator `WRITE_TRUNCATE` (BigQuery) |
| Slice completion markers + torn-load detection | **Built** — verified by killing the loader mid-slice, not by inspection |
| dbt sources reading the *loaded* raw tables | **Wired on BigQuery** — the DuckDB target still reads the CSVs in place, deliberately, for fast credential-free CI |
| Executed BigQuery run | **Done** — 56 windows backfilled, 12 models and 55 tests green (see *Validation status*) |
| Gemini review enrichment + labelled eval set | Planned |
| NL→SQL agent (ceilings already built) | Planned |

Planned means planned. Nothing in this README describes code that does not
exist.

---

## Dataset licence

Brazilian E-Commerce Public Dataset by Olist, CC BY-NC-SA 4.0. Not
redistributed here — `make data` fetches it from Kaggle.
