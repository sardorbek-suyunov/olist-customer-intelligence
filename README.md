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

**79 dbt tests · 118 Python tests · full build in ~5s on DuckDB · $0 to run**

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
make build            # 14 models, 79 tests, against DuckDB
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

79 dbt tests run interleaved with the models (`dbt build`), so a failing test
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

### Incremental where the work is additive

`fct_orders` is the only incremental model, and the asymmetry is the point.

| | Strategy | Why |
|---|---|---|
| `fct_orders` | `merge` on BigQuery, `delete+insert` on DuckDB, keyed on `order_id` | The fact table is the largest object, it grows without bound, and a window of orders is genuinely additive. Reprocessing a window converges instead of appending. |
| `dim_customers` | Full refresh | A Type 2 dimension is **not** additive. One new observation can close an interval that is already written, and a backdated one can split an interval in two — so the correct unit of work is the customer's whole history, not the new rows. |
| `dim_products`, `dim_sellers` | Full refresh | Type 1 over a single observation per key. Same conclusion, arrived at more cheaply. |

The rule: **incremental where the work is additive, full refresh where a new row
can change an old one.** An incremental `dim_customers` keyed on `customer_sk`
would append version rows while leaving the previous version's `valid_to` stale,
and that passes every row-count check while silently overlapping — which is why
`assert_no_overlapping_customer_versions` exists at all.

The window comes from the `run_date` var the DAG already passes, so the scheduler
and a hand-run produce the same filter. The bound is half-open on the low side
only: a late arrival for a prior window is handled by re-running *that* window,
and the merge restates the row. Bounding the top would mean a re-run silently
skipped everything after it.

Proven rather than asserted, against a modified copy of the source — the extract
is static, so a genuine late arrival had to be injected:

```
claim 1  re-run window twice        rows=99,441  duplicates=0
claim 2  late order, prior window   rows=99,442  inserted exactly once
claim 2b re-run that window again   rows=99,442  still exactly once
claim 3  order restated             rows=99,442  updated in place, status='canceled'
```

Each step ran a full `dbt build`, so the grain test reconciled the fact against
source orders every time. On BigQuery the `MERGE` path was exercised separately
and is idempotent over 96.5k merged rows.

---

## Validation status

Honest accounting of what has actually been executed. Both columns have now run;
nothing below is inferred from a compile.

| | DuckDB | BigQuery |
|---|---|---|
| 14 models build | ✅ executed | ✅ executed |
| 79 dbt tests pass | ✅ executed | ✅ executed |
| Normalization parity over 12,818 strings | ✅ executed | ✅ executed |
| Raw backfill reconciles to source | ✅ executed | ✅ executed |
| Slice reload leaves row counts unchanged | ✅ executed | ✅ executed |
| `maximum_bytes_billed` rejects an oversized query | n/a | ✅ **observed firing** |
| One DAG window executed end to end | ✅ executed | ⬜ not yet run |
| Incremental `fct_orders` re-run leaves no duplicates | ✅ executed (delete+insert) | ✅ executed (merge) |

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

Three more surfaced the first time the **orchestrator** ran, having survived
every import check and structural assertion CI makes:

7. **The transform task could never render.** `--vars` was written
   `{{"run_date": ...}}` — doubled braces, which is how you escape a brace in an
   f-string, in a string that is not an f-string. Jinja read `{{` as the start of
   a print statement: *TemplateSyntaxError, expected token 'end of print
   statement', got ':'*.
8. **A manual run produced a zero-width window.** In Airflow 3 a manual trigger
   has no schedule-derived interval, so `data_interval_start` and
   `data_interval_end` both collapse onto the logical date. The DAG passed both
   through and replay correctly refused `--start 2016-09-01 --end 2016-09-01`.
   Scheduled runs would have been fine, which is exactly why nothing caught it.
   The window end is now derived from its start.
9. **`requirements-airflow.txt` could not be installed as documented.** It
   pinned providers against versions the constraints file it tells you to use
   pins differently, so `pip install -r <file> --constraint <that file>` was
   ResolutionImpossible — and the CI job that would have caught this failed at
   its install step on the very first push, which is how the Status table came
   to describe the DAGs as "import-verified in CI".

A tenth showed up in ingestion rather than SQL, and is written up in
[the incident note](docs/incidents/2026-09-11-torn-backfill.md): an interrupted
backfill left a slice half-loaded and reported success, because the
reconciliation guarded `table in counts` and a table that was never reached
contributes no count to disagree with.

`strip_accents` was already adapter-dispatched, and `IS DISTINCT FROM` was
rewritten longhand so the parity test compiles identically everywhere.

**Nine bugs; six of them needed something to actually execute.** Three came from
a static sweep, three from the first BigQuery build, three from the first DAG run.
That ratio is the argument for running things, and against a status table that
says "compile-checked" and means "works".

---

## Review aspect extraction

40,950 of 99,224 reviews carry free text
(41.3%), deduplicating to 35,616 distinct
strings. All of them are labelled against a 16-aspect taxonomy by
`gemini-3.1-flash-lite` with thinking disabled: 50,193
aspect instances, 1.41 per review,
**$3.19** and 85 minutes
for the full pass, zero quarantined.

A cheap model measured honestly, rather than a capable one taken on trust. The
budget is real and small; spending it all on one pass of a better model would
have bought a higher number and no eval, no prompt iteration and no embeddings —
and those are the parts that show judgement. → [DECISIONS 6](docs/DECISIONS.md)

**The labels are committed** (`enrichment/data/*.parquet`, 3.3 MiB). A clone
builds the aspect marts and re-runs the eval with no API key and no spend; the
snapshot doubles as the cache, so a re-run records zero calls and $0.00.

### What the eval can measure, and what it cannot

Ground truth is generated by a stronger model, `gemini-3.8-flash`, run
at the same thinking setting as the model under test so the comparison isolates
capability rather than confounding it with thinking budget. That bounds the
result and the bound is stated rather than absorbed: this measures **agreement
with a stronger model, not accuracy**, and the ceiling is the reference's own
unmeasured error rate. → [DECISIONS 5](docs/DECISIONS.md)

The sample is 600 reviews — 300 random plus
300 chosen by greedy set-cover over per-aspect deficits.
Two strata, because they answer different questions:

| | drawn from | supports |
|---|---|---|
| random | the corpus, ignoring labels | recall, and unbiased prevalence |
| targeted | the model's own positives | precision on rare aspects |

A false negative is a review the model *failed* to label, so it can only appear
in the random stratum. An aspect needs 30 positives **inside that stratum** to
clear the floor, which takes roughly 10% prevalence at this sample size. Exactly
three aspects do. So **recall is reported for 3 of 16
aspects and withheld for the other 13**, which are
named with their counts instead of being scored anyway. Recall for a 0.6% aspect
would need a random stratum near 5,000 hand-adjudicated reviews.

An absent number that says why it is absent is a stronger result than a present
number that cannot be trusted.

### One prompt change, diagnosed before it was made

`product_quality` accounted for 39 of
85 false negatives — 46% of every miss. Three
measurements narrowed the cause before anything was edited:

- **Not global under-labelling.** 1.512 aspects per review against the
  reference's 1.508. The subject labels the same amount, just not the same things.
- **Not a polarity asymmetry.** Recall was 72% on negative text and 76% on
  positive, so it was not "catches complaints, misses praise".
- **The product verdict specifically was being dropped.** In 31 of the 39 misses
  the subject labelled some *other* aspect on the same review. `Os copos são
  lindos e chegaram antes do prazo` got `delivery_fast` alone.

That points at the wording, not the model: "build quality or materials" reads as
a request for a technical judgement, and a plain `Bom produto` does not obviously
match it. **One** change, to one description, so the re-score attributes cleanly.

| | v1 | v2 |
|---|---:|---:|
| false negatives | 85 | **61** |
| false positives | 87 | 94 |
| micro precision | 0.904 | 0.9 |
| micro recall | 0.906 | **0.933** |
| micro F1 | 0.905 | **0.916** |
| exact aspect-set match | 77.8% | **80.2%** |
| sentiment agreement | 94.5% | 95.8% |
| `product_quality` recall | 0.825 | **0.969** |

The before/after with the diagnosis between it is the deliverable. The final
number on its own would not show that the change was reasoned rather than found
by trying things.

### ⚠️ The marts ship v1. The better prompt is measured, not applied.

Every row of `fct_segment_aspect` is stamped
`label_prompt_version = 'v1'`, and a dbt test fails the
build if the labelled data reaching the marts is anything other than the single
run `dbt_project.yml` declares.

v2 scored better and is **not** shipped, for two reasons:

1. **Cost.** Relabelling 35,616 texts at v2 costs about
   $3.19 against a remaining balance smaller than that.
2. **A partial v2 corpus would be a correctness bug, not a compromise.** Aspect
   frequencies would carry mixed provenance, and a real difference in the data
   could not be separated from a prompt artefact. Clean v1 beats mixed.

`PROMPT_VERSION` is part of the cache key precisely so a half-migrated corpus
cannot be built by accident and mistaken for a whole one.

### The circularity limitation, demonstrating itself

The caveat above — that a model-generated reference measures agreement rather
than accuracy — is the kind of thing that reads as boilerplate. This eval
produced a concrete instance of it.

The v2 change did not mention `seller_unresponsive`. Its precision fell anyway,
**0.812 → 0.702**,
on 9 → 17 false
positives.

Reading those 17 disagreements does not settle what happened. Several are
reviews where the customer plainly describes contacting the seller — *"entrei em
contato com o vendedor"*, *"liguei pra reclamar"* — and the **reference** did not
label them. So the regression is either the subject over-applying the aspect or
the reference missing it, and **this eval structurally cannot tell which**:
adjudicating it requires a ground truth better than the reference, which is the
thing the design does not have.

It is left in the table as a disagreement rather than resolved in the subject's
favour. That is the honest handling, and it is worth more than the caveat
paragraph: the limitation is not a risk that might materialise, it is a number
in the results that nothing available can adjudicate. What would fix it is
specific and costed — hand-adjudicating those 17 reviews, or a Pro pass over a
subset (~$0.12) to put reference-against-reference disagreement on record.

### The mart

`fct_segment_aspect` is RFM segment × aspect, one row per pair, complete grid so
an aspect a segment never mentions is an explicit zero rather than a missing row.

**Coverage is a column, not a footnote.** Only
41.0% of orders carry review text, and the share
differs by segment — 37.4% to 43.9%. A segment showing few delivery complaints
might have few complaints, or might be a segment that does not write reviews.
So the mart publishes `review_text_coverage_pct` alongside both denominators,
`aspect_rate_of_reviewed` and `aspect_rate_of_all_orders`. Publishing only the
first invites exactly the mistake coverage exists to prevent.

**Frequency is deliberately not scored.** 96.88% of customers
have exactly one order, so `NTILE(5)` over that column does not find five groups
— it cuts ties into arbitrary blocks that look exactly like real scores.
Segmentation is on Recency and Monetary; frequency travels as a count and a
flag. An honest two-dimensional segmentation beats a three-dimensional one whose
third dimension is noise.

### Embeddings: costed before being spent

686,882 tokens (±1,260 at 95%), **$0.1374**
standard or $0.0687 batched. Measured with `countTokens` over an
800-text sample, fitted against exact character counts, and reconciled against
the input tokens the labelling run was actually billed for — not `chars / 3.5`.

The model is `gemini-embedding-2`, not `gemini-embedding-001` which the plan
originally named: 001 does not appear on Google's pricing page at all, so the
only rates available for it are third-party. `enrichment/pricing.py` leaves it
unpriced on purpose, where it logs $0.00 and warns.

**The binding constraint is not the dollars.** `VECTOR_SEARCH` needs no index —
it falls back to brute force and does not miss unindexed rows — but brute force
scans the whole embedding column, and at 3072 dimensions that column is
834.8 MiB, or **81.5% of
the 1 GiB per-query ceiling this project already enforces on itself**. One added
join trips a guardrail. At 1536 it is 417.4 MiB
(40.8%), which is why the plan truncates —
Matryoshka truncation is a property of the model, not a lossy afterthought.

Worth recording that skipping the index is a choice and not a limitation: the
10 MB floor below which an index silently fails to populate is
87.5× below this table.

---

## The NL→SQL agent

`22 of 25` gold questions answered correctly —
**88.0% execution accuracy** on `gemini-3.1-flash-lite`, for
$0.01201 of Gemini.

Execution accuracy means both statements are **run** and their result sets
compared. Not string similarity: `count(*)` and `sum(1)` are the same answer and
share almost no characters, so scoring on text rewards SQL that resembles the
answer key over SQL that answers the question.

| category | score |
|---|---|
| simple | 10/10 |
| aggregate | 5/5 |
| ranking | 3/3 |
| join | 1/1 |
| **trap** | **3/6** |

**Every failure is a trap question, and everything else is 19/19.** That is the
result, not the headline percentage. `trap` questions are ones where the obvious
SQL returns plausible rows and the wrong number — mostly the grain of
`fct_segment_aspect`, which has one row per (segment, aspect).

- **g08** *"which segment has the highest delivery complaint rate?"* — the agent
  took `MAX(aspect_rate_of_reviewed)` over the delivery aspects where the
  question needs `SUM`. It answered `champions, 9.24`; the truth is
  `loyal, 30.41`. Right shape, wrong segment, nothing about it looks wrong.
- **g22** *"compare champions and hibernating"* — returned 12 rows, one per
  aspect, instead of 2 aggregated. Same grain, not aggregated at all.
- **g09** is borderline and is counted as a failure anyway: the agent returned
  `champions, 15924` where the gold returns `15924`. The value is right and
  there is an extra label column. Kept strict rather than relaxed, because
  loosening a comparison after seeing which cases it fails is how an accuracy
  figure stops meaning anything.

### Two of the fixes on the way to that number were mine, not the model's

The first run scored **11/25**. Most of the gap was defects in the harness and
the prompt, and finding them is the reason the eval exists:

| what was wrong | effect |
|---|---|
| The eval treated any `ORDER BY` in the gold as "order is part of the answer" | Failed 3 correct answers. The gold was ordered for stable output; the questions never asked for an ordering. Now an explicit `ordered` flag per question. |
| The schema prompt was built from the dbt manifest alone | The manifest only carries columns someone wrote YAML for, so the agent saw **3 of `fct_orders`' 17 columns**. `order_value` was invisible, and the agent invented a `fct_order_items` table to compute revenue from — four times. Column list now comes from the warehouse, descriptions from the manifest. |
| `Agent` never passed `allowed_tables` to the guard | Hallucinated tables reached BigQuery as 404s instead of being refused with a message naming what *is* available. |

11 → 14 → 22. The two middle failures were mine; reporting 11/25 as the model's
score would have been wrong in the model's disfavour, and reporting 22/25
without saying what moved would be wrong in mine.

### The controls, and which one actually matters

| layer | what it does |
|---|---|
| `gemini_budget` | prices the call with `countTokens` **before** issuing it, reserves against a shared ledger, settles against real usage |
| `sql_guard` | parses with sqlglot, rejects anything that is not a single `SELECT`, **injects** a `LIMIT` rather than asking for one |
| `bq_safety` | dry-runs for exact bytes, checks both ceilings, sets `maximum_bytes_billed` so BigQuery enforces it server-side |
| **IAM** | the service account holds `dataViewer` on the marts dataset and nothing else |

The last row is the boundary. The three above it run *inside* the application,
so they protect against the model behaving badly, not against the application
behaving badly. The grant survives the code being wrong — which, per the table
above, it was.

---

## One failure mode, seven times

Every bug in this project that survived review shares a shape: **a status
reported by something other than the thing being measured.** Not a wrong answer —
a correct answer to a question nobody meant to ask. Each one was found by
executing something, and each was invisible until then.

| The claim | What was actually measured | How it failed |
|---|---|---|
| `job.output_rows` reports what landed in the partition | Rows *that job wrote*. A second load of the same slice returns the same number whether the partition was replaced or doubled. | The one function whose purpose was verifying idempotency could not distinguish the two states it existed to tell apart. |
| The README reports the test count | A number typed by hand, once, from a report about the data. The README said 68, dbt reports 79, and CI's own comment said 67. | Three sources, three different answers, none of them reading from dbt. |
| `validate_bigquery.py` proves the normalization macro works on BigQuery | Its own transcription of that macro — which stopped describing the pipeline the moment the models moved to `dbt_utils` and its `-` separator. | The validator would have reported success over a broken pipeline. It had already happened once before, with the same file. |
| The Status table reports the DAGs as "import-verified in CI" | An assertion written in a document. The job had never passed: it failed at its install step on the very first push, because the requirements file contradicted the constraints file it tells you to use. | A verification claim about a verification that had never run. |
| `AIRFLOW_EXIT=0` reports that Airflow installed | The **outer** shell's `$?`. A heredoc consumed the backslash, so the exit code came from the previous command rather than from pip. | Airflow was reported installed while `import airflow` raised `ModuleNotFoundError`. The real result was `ResolutionImpossible`. |
| The enrichment cost log reports what the phase spent | Only the calls that went through the pipeline. Three exploratory calls made directly against the client spent $0.0863 that no row recorded. | The log is the thing the README quotes. A total that omits real spend because it was spent while deciding is still an understatement, and it under-reports in the direction nobody checks. |
| `thinking_budget=128` reports a ceiling on thinking | A **hint**. The model returned 1,284 thought tokens against a budget of 128 — 10x over — and thought tokens bill at the output rate. | A cost projection built on that parameter would have been wrong by an order of magnitude, in the expensive direction, on the one number it existed to bound. |

The Airflow one is the clearest, because the gap is widest: a green status
printed while the thing it described did not exist. The thinking budget is the
subtlest, and the most useful to have learned: the parameter is not lying, it
simply does not mean what its name implies, and nothing surfaces the difference
except measuring the result.

The fix is identical in all seven cases, and it is not "be more careful":

- **Count from the table, not from the job** — a partition-pruned `COUNT(*)`.
- **Compile the macro, never transcribe it** — `dbt compile` renders what the
  pipeline runs.
- **Generate the document, do not maintain it** — the README is rendered from
  `docs/figures.json`, so there is no second copy left to disagree.
- **Let the job report its own status** — the CI badge, not a sentence claiming
  the job passes.
- **Verify the exit code with something other than the shell that produced it** —
  and when a status looks too clean, check the thing itself.
- **Record spend where it is spent, not where it is convenient** — the probe
  rows were written into the cost log by hand rather than left out because they
  were exploratory.
- **Measure the parameter's effect, do not trust its name** — one probe call,
  about eight cents, replaced a projection that was out by 10x.

Every control in this repository is an instance of that: the completion marker
that a slice writes only after every table lands, the parity test that re-derives
12,818 strings through the macro instead of trusting a comment, the ceiling
enforced by BigQuery rather than by a prompt. The pattern is worth more than any
individual fix, which is why it is written down here rather than left implicit.

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
| Pre-flight `countTokens` + `max_output_tokens` | [`analytics/gemini_budget.py`](analytics/gemini_budget.py) | worst case known before the call |
| Per-session Gemini ceiling | same | $0.01 / 10 questions |
| Per-day and lifetime ceilings, shared | same | $0.05/day, $2.00 lifetime |
| Untouchable reserve | same | $0.25, never spendable by the demo |
| Pre-computed example answers | [`analytics/demo_examples.py`](analytics/demo_examples.py) | 6 questions, $0 and no scan |

### The demo is behind a public URL, and the key has a balance

The agent calls Gemini on every visitor question. A portfolio link that is dead
because a few hundred people clicked it is the worst outcome this repository can
produce — the link is already on the CV. So the same three-layer shape as
`bq_safety`, applied to the other half of the bill:

**Price the call before making it.** There is no dry run for `generateContent`,
but `countTokens` is free and exact and `max_output_tokens` bounds the other
side, so the *maximum* cost is known before the call is issued and a call that
cannot be afforded is never made. The budget object supplies the output cap as
request config rather than trusting the caller to set it — an unenforced limit
is the "prompt instructions are not a control" mistake in a new costume.

**Reserve, then settle.** `check()` writes the worst case to a shared on-disk
ledger *before* the API call and `record()` settles it against the actual
`usage_metadata` afterwards. Checking without reserving is a check-then-act race
on the one number that must not go negative: two visitors arriving in the window
between check and call would both be told yes against the same balance. Settling
against metadata rather than the estimate is the cost-log lesson applied in
advance instead of after.

**Degrade, do not error.** When a ceiling trips the demo falls back to the
pre-computed examples with an explanation of which ceiling and when it resets. A
visitor who never clicks "ask your own question" cannot tell the difference,
because the default state was already free.

The last one is the control that actually matters. Caps stop a demo dying
expensively; they do not stop it being useless once they trip.

Two of these were written because a test failed, not because they were designed
in: the per-session lock was originally per-*instance*, which looks like mutual
exclusion and provides none, and the ledger's shared temp filename was itself a
race. Both were caught by a ten-thread concurrency test, which is the only
reason they are not still there.

### Secrets are scanned across the whole history, not the working tree

A credential committed once is in the pack forever, and the window to rewrite
history cheaply closes the moment someone clones it. So CI scans **every commit**
(`--no-current`, `fetch-depth: 0`), not the tip — a shallow checkout would
produce a green check describing a scan that never looked at anything.

Verified that it can fail before trusting that it passes: a throwaway repo with
a planted AWS key and a Gemini-shaped key, committed and then *deleted*, is
flagged in history. A scanner reporting zero findings and a scanner that is
silently broken look identical from the outside.

Result here: clean. `.env` has never been committed, and no path matching a
credential pattern has ever been added.

The scanner is installed in isolation, and that is not a preference:
`trufflehog3` pins `attrs==20.3.0`, which is incompatible with the `jsonschema`
that dbt depends on. Installing it into the project environment downgrades
`attrs` and every subsequent `dbt` command dies with a traceback that names
`jsonschema` and never mentions the scanner that caused it. `make secrets` runs
it through `pipx`; CI gives it a job on a runner that never installs
`requirements.txt`.

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
analytics/          NL->SQL spend ceilings, Gemini demo budget, cached examples
                    (+ 61 unit tests)
dashboard/          Streamlit app + committed Parquet snapshot of the marts
docs/adr/           Architecture decision records
docs/DECISIONS.md   Judgement calls and their reasoning -- distinct from the ADRs
docs/               Generated data profiling report
enrichment/         Gemini aspect labelling: taxonomy, client, store, pricing
enrichment/data/    Committed labels -- a clone runs the eval for $0.00
enrichment/eval/    Eval sample design and v1/v2 scores
ingestion/          replay.py -- time-sliced, referentially-consistent extract
orchestration/      Airflow 3 DAGs + DAG integrity tests
scripts/            download_data, profile_dataset, eval_score, export_*
transform/          dbt project: staging -> intermediate -> marts
```

The dashboard reads the **committed Parquet snapshot** by default, with a
sidebar toggle for live BigQuery. A public demo that 500s six months after it
was shared because a service-account key expired is worse than no demo.

---

## Status

| Component | State |
|---|---|
| Replay harness, staging, SCD2 marts, 79 dbt tests | **Built and passing** |
| DuckDB + BigQuery dual targets, cost ceilings | **Built** |
| Streamlit dashboard + committed snapshot | **Built** |
| Airflow DAGs + integrity tests | **Executed** — one backfill window end to end, all 7 tasks green; 16 integrity tests |
| `ingestion/load.py` (slice → warehouse) | **Built and proven idempotent on both** — delete-then-insert (DuckDB) / partition-decorator `WRITE_TRUNCATE` (BigQuery) |
| Slice completion markers + torn-load detection | **Built** — verified by killing the loader mid-slice, not by inspection |
| dbt sources reading the *loaded* raw tables | **Wired on BigQuery** — the DuckDB target still reads the CSVs in place, deliberately, for fast credential-free CI |
| Executed BigQuery run | **Done** — 56 windows backfilled, 14 models and 79 tests green (see *Validation status*) |
| Gemini review enrichment | **Executed** — 35,616 texts labelled at vv1 for $3.19, 0 quarantined, labels committed |
| Per-aspect eval + v1→v2 prompt iteration | **Executed** — 600-review sample, micro F1 0.905 → 0.916; recall reported for 3 of 16 aspects and withheld for 13 |
| `fct_segment_aspect` (RFM × aspect, coverage as a column) | **Built and tested** — complete grid, provenance-stamped, 79 dbt tests green |
| Gemini demo budget (session/day/lifetime + cached answers) | **Built and tested** — 61 unit tests including a ten-thread concurrency check |
| Review embeddings (`gemini-embedding-2`, 1536-d) | **Costed, not yet run** — 686,882 tokens measured (±1,260), $0.1374 |
| NL→SQL agent: parsed SQL guard, injected LIMIT, both ceilings | **Built and tested** — 61 unit tests; gold set of 25 question/SQL pairs |
| Secret scan over full git history, in CI | **Executed** — clean; verified against a planted key that the scan can fail |

Planned means planned. Nothing in this README describes code that does not
exist.

---

## Dataset licence

Brazilian E-Commerce Public Dataset by Olist, CC BY-NC-SA 4.0. Not
redistributed here — `make data` fetches it from Kaggle.
