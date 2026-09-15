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

**{{dbt_tests}} dbt tests · {{python_tests}} Python tests · full build in ~5s on DuckDB · $0 to run**

{{demo_callout}}

Ask the warehouse a question in English. The agent writes BigQuery SQL, a parser
refuses anything that is not a single `SELECT` and **injects** the `LIMIT`, and
the query is priced before it runs — {{nl2sql_matched}}/{{nl2sql_gold_total}}
on a gold set, for ${{nl2sql_usd}} of Gemini across the whole evaluation.

![The NL→SQL agent answering a question: the generated SQL with the guard's
injected LIMIT, and the cost of the call](docs/img/nl2sql-demo.png)

Not a mockup and not pasted in by hand. `make screenshot` drives the running app
with Selenium, asks that question, and refuses to save unless the panel shows all
three things this README says it shows — the SQL, the cost, and the injected
`LIMIT`. The numbers in it were produced by the same build as the numbers below.

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

> **{{orders_on_noncurrent_version}} of {{orders_total}} orders belong to a superseded
> version. {{misattributed_orders}} of those ({{misattributed_pct}}%, across
> {{misattributed_customers}} customers) receive materially different attributes from a
> naive join to the current row — {{misattributed_wrong_state}} of them the wrong _state_.**

The other {{orders_returned_to_previous_value}} belong to customers who moved away and
came back, so the current row happens to be correct for them. Worth separating: the
as-of join is load-bearing for {{orders_on_noncurrent_version}} orders, but only
{{misattributed_orders}} of them would actually be wrong without it.

Small, and completely silent: without the as-of join those orders move revenue
between regions in every geographic report. → [ADR 0001](docs/adr/0001-derive-scd2-customers.md)

`dim_products` and `dim_sellers` are **Type 1 by necessity** — one observation
per key means no snapshot strategy can ever yield a second version. Shipping
empty snapshot configs to look thorough would be worse than not having them.

---

## Architecture

![Architecture: CSVs through replay, Airflow-orchestrated load and dbt build, into
staging, intermediate and marts, with Gemini enrichment and the Streamlit demo.
Green nodes run on both engines, blue on BigQuery only, gold on DuckDB
only.](docs/img/architecture.svg)

**Green runs on both engines from the same models; blue is BigQuery only; gold is
the DuckDB-only demo path.** The colouring is the point of the picture — it is
the one part of this design that a file listing does not show.

The diagram source is [`docs/architecture.mmd`](docs/architecture.mmd) and the
SVG is rendered from it by `make diagram`. A diagram is the easiest thing in a
repository to let rot: nothing compiles it, and a stale box looks exactly like a
correct one. So `test_architecture_diagram.py` asserts every model and dataset
named in it against the dbt manifest, every mart against the diagram, and — since
an SVG is text — that the committed image still contains the labels its source
declares. Rename a model and the build fails rather than the picture quietly
lying. That check is why an image is allowed in this repository at all.

Orchestrated by two Airflow 3 DAGs (`orchestration/dags/olist_batch.py`), each a
thin `BashOperator` wrapper over CLI-invokable modules — so swapping Airflow for
Dagster or cron means rewriting one file and nothing else.

### The model documentation

{{dbt_docs_link}}

It is generated in the **same CI job that just built and tested the project**,
not by a workflow that rebuilds independently. A second build would be a second
answer: the published catalogue would describe a run nothing else verified, and
the two could disagree with nothing to notice. The column types and row counts in
it come from querying the warehouse, so they are measured rather than declared —
the same rule as every other number here.

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
| `assert_normalize_macro_matches_python` | The SQL macro and the Python helper agree on all **{{parity_rows}}** distinct location strings. Executed on **both** engines (see *Validation status*). |

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
| {{dbt_models}} models build | ✅ executed | ✅ executed |
| {{dbt_tests}} dbt tests pass | ✅ executed | ✅ executed |
| Normalization parity over {{parity_rows}} strings | ✅ executed | ✅ executed |
| Raw backfill reconciles to source | ✅ executed | ✅ executed |
| Slice reload leaves row counts unchanged | ✅ executed | ✅ executed |
| `maximum_bytes_billed` rejects an oversized query | n/a | ✅ **observed firing** |
| One DAG window executed end to end | ✅ executed | ⬜ not yet run |
| Incremental `fct_orders` re-run leaves no duplicates | ✅ executed (delete+insert) | ✅ executed (merge) |

**A cross-engine claim tested on one engine is not a cross-engine claim.** That
table was true and one model in it had never been compiled for BigQuery at all:
`GCP_PROJECT_ID` was unset, `profiles.yml` defaulted it to `''`, and every
BigQuery command failed with a server message naming nothing. With that fixed,
`fct_segment_aspect` failed immediately on a dialect difference the macro was
supposed to handle — the lateral `UNNEST` alias is `as a(aspect)` on DuckDB and
`as aspect` on BigQuery, and the macro dispatched only the extraction function
because the alias form had been "verified on both" by testing it on DuckDB.

Both engines now build all {{dbt_models}} models and {{dbt_tests}} tests. The
default is gone, so a missing project id now says
`Env var required but not provided: 'GCP_PROJECT_ID'`.

The SCD2 figures come out identical on both engines: {{scd2_change_events}} change events
across {{scd2_customers_changed}} customers, {{dim_customers_current}} current +
{{dim_customers_historical}} historical = {{dim_customers_rows}} rows,
{{misattributed_orders}} mis-attributed orders and {{misattributed_wrong_state}} of them to
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
   `valid_from` is floored to 1900-01-01 — which would have put {{dim_customers_current}} of
   {{dim_customers_rows}} rows outside the legal range. Partitioning removed (it was also the
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

{{reviews_with_text}} of {{review_rows}} reviews carry free text
({{reviews_with_text_pct}}%), deduplicating to {{review_texts_distinct}} distinct
strings. All of them are labelled against a 16-aspect taxonomy by
`{{shipped_label_model}}` with thinking disabled: {{enrichment_aspect_instances_v1}}
aspect instances, {{enrichment_aspects_per_review_v1}} per review,
**${{enrichment_corpus_cost_usd_v1}}** and {{enrichment_corpus_minutes_v1}} minutes
for the full pass, zero quarantined.

A cheap model measured honestly, rather than a capable one taken on trust. The
budget is real and small; spending it all on one pass of a better model would
have bought a higher number and no eval, no prompt iteration and no embeddings —
and those are the parts that show judgement. → [DECISIONS 6](docs/DECISIONS.md)

**The labels are committed** (`enrichment/data/*.parquet`, 3.3 MiB). A clone
builds the aspect marts and re-runs the eval with no API key and no spend; the
snapshot doubles as the cache, so a re-run records zero calls and $0.00.

### What the eval can measure, and what it cannot

Ground truth is generated by a stronger model, `{{eval_reference_model_v1}}`, run
at the same thinking setting as the model under test so the comparison isolates
capability rather than confounding it with thinking budget. That bounds the
result and the bound is stated rather than absorbed: this measures **agreement
with a stronger model, not accuracy**, and the ceiling is the reference's own
unmeasured error rate. → [DECISIONS 5](docs/DECISIONS.md)

The sample is {{eval_sample_size}} reviews — {{eval_random_stratum}} random plus
{{eval_targeted_stratum}} chosen by greedy set-cover over per-aspect deficits.
Two strata, because they answer different questions:

| | drawn from | supports |
|---|---|---|
| random | the corpus, ignoring labels | recall, and unbiased prevalence |
| targeted | the model's own positives | precision on rare aspects |

A false negative is a review the model *failed* to label, so it can only appear
in the random stratum. An aspect needs 30 positives **inside that stratum** to
clear the floor, which takes roughly 10% prevalence at this sample size. Exactly
three aspects do. So **recall is reported for 3 of {{eval_aspects_scored_v1}}
aspects and withheld for the other {{eval_recall_not_reported_v1}}**, which are
named with their counts instead of being scored anyway. Recall for a 0.6% aspect
would need a random stratum near 5,000 hand-adjudicated reviews.

An absent number that says why it is absent is a stronger result than a present
number that cannot be trusted.

### One prompt change, diagnosed before it was made

`product_quality` accounted for {{eval_product_quality_fn_v1}} of
{{eval_false_negatives_v1}} false negatives — 46% of every miss. Three
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
| false negatives | {{eval_false_negatives_v1}} | **{{eval_false_negatives_v2}}** |
| false positives | {{eval_false_positives_v1}} | {{eval_false_positives_v2}} |
| micro precision | {{eval_micro_precision_v1}} | {{eval_micro_precision_v2}} |
| micro recall | {{eval_micro_recall_v1}} | **{{eval_micro_recall_v2}}** |
| micro F1 | {{eval_micro_f1_v1}} | **{{eval_micro_f1_v2}}** |
| exact aspect-set match | {{eval_exact_match_pct_v1}}% | **{{eval_exact_match_pct_v2}}%** |
| sentiment agreement | {{eval_sentiment_agreement_pct_v1}}% | {{eval_sentiment_agreement_pct_v2}}% |
| `product_quality` recall | {{eval_product_quality_recall_v1}} | **{{eval_product_quality_recall_v2}}** |

The before/after with the diagnosis between it is the deliverable. The final
number on its own would not show that the change was reasoned rather than found
by trying things.

### ⚠️ The marts ship v1. The better prompt is measured, not applied.

Every row of `fct_segment_aspect` is stamped
`label_prompt_version = '{{shipped_prompt_version}}'`, and a dbt test fails the
build if the labelled data reaching the marts is anything other than the single
run `dbt_project.yml` declares.

v2 scored better and is **not** shipped, for two reasons:

1. **Cost.** Relabelling {{enrichment_texts_labelled_v1}} texts at v2 costs about
   ${{enrichment_corpus_cost_usd_v1}} against a remaining balance smaller than that.
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
**{{eval_seller_unresponsive_precision_v1}} → {{eval_seller_unresponsive_precision_v2}}**,
on {{eval_seller_unresponsive_fp_v1}} → {{eval_seller_unresponsive_fp_v2}} false
positives.

Reading those {{eval_seller_unresponsive_fp_v2}} disagreements does not settle what happened. Several are
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
specific and costed — hand-adjudicating those {{eval_seller_unresponsive_fp_v2}} reviews, or a Pro pass over a
subset (~$0.12) to put reference-against-reference disagreement on record.

### The mart

`fct_segment_aspect` is RFM segment × aspect, one row per pair, complete grid so
an aspect a segment never mentions is an explicit zero rather than a missing row.

**Coverage is a column, not a footnote.** Only
{{orders_with_review_text_pct}}% of orders carry review text, and the share
differs by segment — 37.4% to 43.9%. A segment showing few delivery complaints
might have few complaints, or might be a segment that does not write reviews.
So the mart publishes `review_text_coverage_pct` alongside both denominators,
`aspect_rate_of_reviewed` and `aspect_rate_of_all_orders`. Publishing only the
first invites exactly the mistake coverage exists to prevent.

**Frequency is deliberately not scored.** {{single_order_customer_pct}}% of customers
have exactly one order, so `NTILE(5)` over that column does not find five groups
— it cuts ties into arbitrary blocks that look exactly like real scores.
Segmentation is on Recency and Monetary; frequency travels as a count and a
flag. An honest two-dimensional segmentation beats a three-dimensional one whose
third dimension is noise.

### Embeddings: executed, and the search verified

All **{{review_texts_distinct}}** distinct texts embedded at
{{vector_search_dimensions}} dimensions. **{{embedding_tokens}} tokens,
${{embedding_usd_standard}}** — against a pre-spend estimate of 686,882 ± 1,260,
so the measurement was out by 28 tokens.

`make embed-reconcile` compares the cost log against the vectors that actually
exist: **gap zero**, every vector accounted for.

**Do the vectors mean anything?** "35,616 rows written" is not a result — a run
that silently produced garbage writes exactly the same number of rows. Nearest
neighbours, with the aspect labels alongside as an independent check:

```
QUERY "o produto chegou quebrado"        QUERY "entrega atrasou muito"
 0.99  o produto veio quebrado            0.96  atrasou a entega
 0.95  produto veio danificado            0.96  muita demora na entrega
 0.94  produto chegou no prazo porem      0.94  demora com a entrega
       veio quebrado                      0.92  entrega muito atrasada.
 0.93  produto veio com defeito           0.92  ta demorando a entrega nao
 0.93  recebi o produto danificado              resebi minha compra ainda
 → 5/5 labelled product_defect           → 5/5 labelled delivery_late
```

Not everything is that clean, and the weak case is worth stating: *"o vendedor
nao respondeu minhas mensagens"* returns only 2 of 5 labelled
`seller_unresponsive`, the rest `not_received` — and the similarity drops to
0.83, which correctly signals the weaker match.

### Does a demo-shaped VECTOR_SEARCH fit under the ceiling?

Yes, with 2.4× to spare — but the interesting answer is *which* ceiling binds.

| query | billed | % of 1 GiB/query |
|---|---:|---:|
| bare `VECTOR_SEARCH` | {{vector_search_bare_mib}} MiB | — |
| **full demo shape** (search + text + orders + aspects) | **{{vector_search_worst_mib}} MiB** | **{{vector_search_pct_ceiling}}%** |

Executed, not estimated. That distinction is load-bearing here: a dry run
approved two queries during this measurement that **could not run at all** — one
against a table whose arrays had silently loaded empty, one with a zero probe
vector. Both times it returned a comfortable number.

**The per-query ceiling is not the constraint. The 5 GiB session budget is:** it
allows only **{{vector_search_per_session}} searches per session**. At 3072
dimensions it would be five. The truncation to 1536 was chosen on this
arithmetic before the run, and the measurement confirms it.

### Embeddings: costed before being spent

{{embedding_tokens}} tokens (±{{embedding_tokens_ci95}} at 95%), **${{embedding_usd_standard}}**
standard or ${{embedding_usd_batch}} batched. Measured with `countTokens` over an
800-text sample, fitted against exact character counts, and reconciled against
the input tokens the labelling run was actually billed for — not `chars / 3.5`.

The model is `{{embedding_model}}`, not `gemini-embedding-001` which the plan
originally named: 001 does not appear on Google's pricing page at all, so the
only rates available for it are third-party. `enrichment/pricing.py` leaves it
unpriced on purpose, where it logs $0.00 and warns.

**The binding constraint is not the dollars.** `VECTOR_SEARCH` needs no index —
it falls back to brute force and does not miss unindexed rows — but brute force
scans the whole embedding column, and at 3072 dimensions that column is
{{embedding_table_mib_3072}} MiB, or **{{embedding_pct_query_ceiling_3072}}% of
the 1 GiB per-query ceiling this project already enforces on itself**. One added
join trips a guardrail. At 1536 it is {{embedding_table_mib_1536}} MiB
({{embedding_pct_query_ceiling_1536}}%), which is why the plan truncates —
Matryoshka truncation is a property of the model, not a lossy afterthought.

Worth recording that skipping the index is a choice and not a limitation: the
10 MB floor below which an index silently fails to populate is
{{embedding_x_index_floor_3072}}× below this table.

---

## The NL→SQL agent

`{{nl2sql_matched}} of {{nl2sql_gold_total}}` gold questions answered correctly —
**{{nl2sql_accuracy_pct}}% execution accuracy** on `{{nl2sql_model}}`, for
${{nl2sql_usd}} of Gemini, on prompt `{{nl2sql_prompt_version}}`.
{{demo_availability}}

Execution accuracy means both statements are **run** and their result sets
compared. Not string similarity: `count(*)` and `sum(1)` are the same answer and
share almost no characters, so scoring on text rewards SQL that resembles the
answer key over SQL that answers the question.

| category | score |
|---|---|
| simple | {{nl2sql_simple_matched}}/{{nl2sql_simple_total}} |
| aggregate | {{nl2sql_aggregate_matched}}/{{nl2sql_aggregate_total}} |
| ranking | {{nl2sql_ranking_matched}}/{{nl2sql_ranking_total}} |
| join | {{nl2sql_join_matched}}/{{nl2sql_join_total}} |
| **trap** | **{{nl2sql_trap_matched}}/{{nl2sql_trap_total}}** |

**Every failure is a trap question, and everything else is 19/19.** That is the
result, not the headline percentage. `trap` questions are ones where the obvious
SQL returns plausible rows and the wrong number — mostly the grain of
`fct_segment_aspect`, which has one row per (segment, aspect).

{{nl2sql_mismatch_count}} remain: {{nl2sql_mismatch_ids}}. Both are the same
shape — the correct values with an extra context column — and both are described
in full below, along with why they were not made to pass.

The trap that the prompt *did* fix is worth naming, because it is the one that
was dangerous. **g08** *"which segment has the highest delivery complaint rate?"*
took `MAX(aspect_rate_of_reviewed)` across the delivery aspects where the
question needs `SUM`, and answered `champions, 9.24` against a truth of
`loyal, 30.41`. Wrong segment, plausible number, and nothing about the output
looks wrong — which is why the grain rule in the prompt is written the way it is.

### 11 → 14 → 22 → 23, and three of the four steps were my bugs

The first run scored **11/25**. That number was mostly wrong, and wrong against
the model. Finding out why is the reason the eval exists:

| step | what changed | score |
|---|---|---|
| **11/25** | first run | — |
| → **14/25** | *my bug.* The eval treated any `ORDER BY` in the gold as "order is part of the answer". It was not — the gold was ordered so its output would be stable, and three **correct** answers were failed for returning the same rows in a different order. Ordering significance is now an explicit per-question flag. | +3 |
| → **22/25** | *my bug.* The schema prompt was built from the dbt manifest alone, and the manifest only carries columns someone wrote YAML for — so the agent was shown **3 of `fct_orders`' 17 columns**. `order_value` was invisible, and it invented a `fct_order_items` table to compute revenue from, four times. That reads as hallucination and is nearly the opposite. Column list now comes from the warehouse, descriptions from the manifest. *(Also: `Agent` never passed `allowed_tables` to the guard, so invented tables reached BigQuery as 404s instead of actionable refusals.)* | +8 |
| → **23/25** | *the model's turn.* One targeted prompt change, after diagnosis: all three remaining failures were the same aggregate-across-aspect-grain error. The rule covered reading a **segment-level** column (`DISTINCT`/`MAX`) and said nothing about an **aspect-level** measure, which needs the opposite — `SUM` across the aspects in the group. | +1 |

Reporting 11/25 would have been wrong in the model's disfavour. Reporting 23/25
without saying what moved would be wrong in mine.

### The {{nl2sql_mismatch_count}} that remain, and why I did not make them pass

Both are now the *same* shape: the agent returns the **correct values** with an
extra context column. Values below are from the stamped `{{nl2sql_prompt_version}}`
artifact — the marts are rebuilt between runs, so quoting an earlier one here
would print numbers that no build ever produced.

```
g09  "How many customers are in the champions segment?"
     gold   15935
     agent  champions, 15935          ← right number, plus the label

g22  "Compare champions and hibernating on delivery complaint rate"
     gold   champions 26.8742 · hibernating 20.3745
     agent  champions 42.23 26.8742 · hibernating 40.11 20.3745
                    ↑ coverage, unasked but not wrong
```

Neither is wrong. Both are counted as failures anyway.

Loosening the comparison **after** seeing exactly which cases it fails is how an
accuracy figure stops meaning anything — the change would be indistinguishable
from tuning the metric until the number improved. The rule was fixed before the
run; it stays fixed after it. {{nl2sql_mismatch_count}} marks is a cheap price
for that, and the discipline is more of a result than the marks would have been.

### A question it answers confidently and wrongly

Everything above is about the agent writing SQL that is *refused*. This is the
other failure, and it is the worse one: SQL that is accepted, executed, and
wrong, with every guardrail passing correctly because none of them is looking at
this.

Asked **"which sellers have the longest average delivery times?"** — a natural
question, the kind a visitor types first — the agent returned 200 rows. Every
one of them showed `12.4873`.

```sql
SELECT s.seller_id, s.seller_city, s.seller_state,
       avg(o.delivery_days) AS avg_delivery_days
FROM olist_marts.fct_orders AS o
JOIN olist_marts.dim_sellers AS s
  ON o.seller_count > 0          -- ← not a join condition
GROUP BY 1, 2, 3
ORDER BY avg_delivery_days DESC
LIMIT 200
```

`fct_orders` is at order grain and carries `seller_count`, a **count**. It has no
`seller_id`. `dim_sellers` has no order key. The order-to-seller relationship
lives in `order_items`, which is not a mart — so **the question is not answerable
from this warehouse at all**.

The agent did not say so. It found two tables whose names matched the question,
could not find a key, and used the only predicate available: `o.seller_count > 0`,
true for essentially every order. That is a cross join wearing a join's clothes.
`12.4873` is the global average delivery time, repeated once per seller, sorted
by a column that is the same value all the way down.

**Now count what objected.** The SQL is a single `SELECT`. It reads two permitted
tables. Every function is on the allowlist. The `LIMIT` was injected and applied.
The call was priced before it was issued and cost $0.00062. It executed in
milliseconds, well inside the timeout, and returned exactly 200 rows. The guard,
the budget, the ceilings and the row cap all did precisely what they are for, and
**not one of them is capable of noticing this**, because each asks whether the
statement is *safe* and none asks whether it is *true*.

It is also the hardest kind to catch by eye: right shape, plausible magnitude,
real seller names down the left. You would have to know the schema to see it, and
a visitor does not.

This is **not fixed**, deliberately. The honest options are all worse than
documenting it: a `seller_items` mart would answer this one question and not the
next unanswerable one; a prompt rule naming this join would be tuning against a
case already seen, which the eval section rejects for the same reason; and a
generic "refuse joins without a key relationship" is a real feature with a real
design, not a patch. What is written down here costs nothing and is true —
[decision 17](docs/DECISIONS.md).

It belongs in the same document as the other twelve failures, and it is a
different shape from all of them. Those were *a status reported by something
other than the thing being measured*. This is a **complete, correct-looking
answer to a question the data cannot answer**, produced by a system in which
every control passed. The guardrails bound the blast radius of a wrong query.
They do not make the answer right, and a demo that implies otherwise is selling
something.

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

### The deployed demo does not have that boundary

That table describes the **BigQuery** path. The public demo runs on DuckDB over
committed Parquet, and the IAM row is not weakened there — **it is absent**.
There is no service account, no grant, and no dataset permission. What replaced
it, initially, was the process's own filesystem privileges.

DuckDB permits a great deal that is not DML, so a statement-type check does not
cover the gap. Measured against the code as it stood, not assumed:

| attack | what the guard did | what actually stopped it |
|---|---|---|
| `COPY (SELECT * FROM fct_orders) TO '/app/x.csv'` | **allowed it** | nothing — it wrote 53 KB to disk |
| `SELECT * FROM read_csv('/app/.env')` | refused | the *table* allowlist, which this guard's own docstring called "**NOT** the security boundary" |
| `SELECT getenv('GEMINI_API_KEY')` | **allowed it** | DuckDB 1.5.5 has no `getenv`. Luck, one release from expiring |
| `INSTALL httpfs; LOAD httpfs;` | refused | the one-statement rule, incidentally |
| `ATTACH 'https://…/x.db'` | refused | a BigQuery parse error, incidentally |

Three of those five were refused for reasons that had nothing to do with anyone
deciding they should be. **A control that happens to catch an attack is not a
control**, and the variants that route around each accident — `getenv` with a
real table in the `FROM`, `COPY` with a permitted table, `read_text` as a scalar —
passed everything.

So the execution environment is now layered underneath the parser:

| layer | what it does | proved by |
|---|---|---|
| `read_only=True` | the connection cannot create, write or attach | `CREATE TABLE` refused |
| `enable_external_access=false` | no file reads, no file writes, no extension installs, no HTTP | `read_csv` on a real CSV returns `PermissionException` |
| `lock_configuration=true` | set on the next line, so a query cannot undo the previous one | `SET enable_external_access = true` refused |
| function **allowlist** | a vocabulary of analytic SQL, not a list of attacks someone thought of | `getenv`, `read_text`, `read_blob` refused by name |
| query timeout | one shared connection, so a slow query is everyone's outage | a cartesian join is interrupted, not waited on |

Two details that are the difference between this working and looking like it
works. The snapshot tables are **materialised into the database** rather than
left as views over `read_parquet(...)` — with external access off, the snapshot's
own Parquet is a file like any other, so views would break and the temptation
would be to leave the door open for them. And `SnapshotRunner` **attacks itself
at startup** and refuses to serve if any probe succeeds: asserting that a `SET`
statement did not raise only establishes that DuckDB accepted the words.

Every row above is a test in `analytics/tests/test_snapshot_security.py`, and
each asserts a **refusal** rather than a setting. Each was also verified to fail
when its control is removed — the same standard `maximum_bytes_billed` was held
to, which was not believed until BigQuery was seen rejecting a query with it.

---

## One failure mode, twelve times

Every bug in this project that survived review shares a shape: **a status
reported by something other than the thing being measured.** Not a wrong answer —
a correct answer to a question nobody meant to ask. Each one was found by
executing something, and each was invisible until then.

| The claim | What was actually measured | How it failed |
|---|---|---|
| `job.output_rows` reports what landed in the partition | Rows *that job wrote*. A second load of the same slice returns the same number whether the partition was replaced or doubled. | The one function whose purpose was verifying idempotency could not distinguish the two states it existed to tell apart. |
| The README reports the test count | A number typed by hand, once, from a report about the data. The README said 68, dbt reports {{dbt_tests}}, and CI's own comment said 67. | Three sources, three different answers, none of them reading from dbt. |
| `validate_bigquery.py` proves the normalization macro works on BigQuery | Its own transcription of that macro — which stopped describing the pipeline the moment the models moved to `dbt_utils` and its `-` separator. | The validator would have reported success over a broken pipeline. It had already happened once before, with the same file. |
| The Status table reports the DAGs as "import-verified in CI" | An assertion written in a document. The job had never passed: it failed at its install step on the very first push, because the requirements file contradicted the constraints file it tells you to use. | A verification claim about a verification that had never run. |
| `AIRFLOW_EXIT=0` reports that Airflow installed | The **outer** shell's `$?`. A heredoc consumed the backslash, so the exit code came from the previous command rather than from pip. | Airflow was reported installed while `import airflow` raised `ModuleNotFoundError`. The real result was `ResolutionImpossible`. |
| The enrichment cost log reports what the phase spent | Only the calls that went through the pipeline. Three exploratory calls made directly against the client spent $0.0863 that no row recorded. | The log is the thing the README quotes. A total that omits real spend because it was spent while deciding is still an understatement, and it under-reports in the direction nobody checks. |
| `thinking_budget=128` reports a ceiling on thinking | A **hint**. The model returned 1,284 thought tokens against a budget of 128 — 10x over — and thought tokens bill at the output rate. | A cost projection built on that parameter would have been wrong by an order of magnitude, in the expensive direction, on the one number it existed to bound. |
| `GCP_PROJECT_ID` defaults to `''` when unset | An empty project id, accepted by the client and sent to BigQuery, which answers `Database Error: Request couldn't be served.` | Every `dbt --target bigquery` command failed for a session while `bigquery.Client()` worked on the same machine — because that falls back to the project in ADC. A missing config read as a broken adapter. The answer was in the console URL dbt prints: `?project=&j=...` |
| A `REPEATED FLOAT64` load schema reports 35,616 rows written | 35,616 rows of **empty arrays**. The job succeeded, the table reported 3.4 MiB, and `array_length` was 0 on every row. | The inferred schema fails loudly with the wrong type. This one succeeds, and is the version that would have shipped. |
| A BigQuery **dry run** reports what a query will scan | The shape of the statement, not the contents of the table. Against those empty arrays it returned `1.2 MiB, 0.1% of the ceiling, OK`. | The query cannot execute at all — "Dimension of column embedding does not match". A byte measurement built on dry runs reported a comfortable pass on a table that could not be searched. |
| The ceiling script reports the query fits | `Worst case 0.0 B, ∞x inside the per-query ceiling` — computed over zero rows. | **A safety check reporting safe because there was nothing to check**, written by the script whose entire purpose was to measure that ceiling. The sharpest instance in this table, and self-inflicted. |
| `make readme-check` reports the README is current | That README.md matches README.template.md rendered against `docs/figures.json`. Not that the figures describe the code that ships. `profile_dataset.py` read the agent's scores from a hand-typed path, `nl2sql_v1.json`; the prompt moved to v2 and the path did not. | The README reported the previous prompt's 22/25 while shipping the agent that scores 23/25, and named three failing questions four paragraphs above describing two. **The drift gate passed on every run**, because the render was faithful to figures that were faithful to a superseded file. |

The Airflow one is the clearest, because the gap is widest: a green status
printed while the thing it described did not exist. The thinking budget is the
subtlest: the parameter is not lying, it simply does not mean what its name
implies, and nothing surfaces the difference except measuring the result.

**The last one is the sharpest, and it is mine.** A script written to check
whether a query fits under a ceiling printed that it fit by an infinite margin,
because the table it measured was empty. Every other row in this table is a
control that reported the wrong thing; that row is a control that reported
*safe* because there was nothing to check. It now refuses to report on an empty
table — which is the only fix that distinguishes "measured and fine" from
"measured nothing".

**The twelfth is the same error committed by the control built to prevent it.**
`make readme-check` exists because the README's numbers had drifted three times.
It works: the README cannot disagree with `docs/figures.json`. But it compares a
document to figures, and the drift was *inside* the figures — a hand-typed
filename pointing at the previous prompt's scores. The gate could not have
failed, because the class of error it detects does not include this one, and a
green check is read as "correct" rather than "consistent with something I did
not verify". That is the ceiling script again: a check reporting safe over a
question it was never able to ask. The fix is the same both times — make the
check capable of failing. The eval artifact is now named from the shipped prompt
version and stamped with a fingerprint of the prompt text, so a bump without a
re-run finds no file and an edit without a bump fails the stamp.

The fix is identical in all twelve cases, and it is not "be more careful":

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
- **Let a missing value be missing** — an `env_var` default turned an absent
  project id into a server error naming nothing. Without the default, dbt says
  `Env var required but not provided: 'GCP_PROJECT_ID'`.
- **Verify the data landed, not that the load returned** — `array_length` on
  every row, after the job reports success.
- **Execute the thing you are measuring** — a plan is not a result, and a
  measurement of nothing is not a pass.
- **Name the artifact after what produced it, and stamp it** — a path typed by
  hand is a claim about provenance that nothing checks. The score file is now
  derived from the prompt version and carries a hash of the prompt text, so the
  README cannot quote a run that does not describe the shipped agent.
- **Ask what your green check is unable to see** — the generated README made a
  whole class of drift impossible and left this one untouched. A control is only
  as good as the question it can fail on.

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

## What this cost

| | | |
|---|---:|---|
| enrichment cost log | ${{spend_logged_usd}} | measured, per call |
| demo ledger (NL→SQL + agent eval) | ${{spend_demo_ledger_usd}} | measured, {{spend_demo_ledger_calls}} calls |
| pilots predating the log | ${{spend_pre_log_usd}} | **remembered**, from a console reading |
| **total** | **${{spend_total_usd}}** | of a ${{spend_ceiling_usd}} ceiling |
| remaining | ${{spend_remaining_usd}} | |

**${{spend_measured_usd}} of that is measured per call. ${{spend_pre_log_usd}} is
not**, and that is stated rather than folded in. The earliest pilots ran before
the cost log existed, so no amount of summing recovers them; the figure comes
from a console reading taken at the time and is the one number here that cannot
be regenerated. There is no API that returns "how much have I spent" without a
BigQuery billing export configured in advance — `make spend --verify` prints the
console URL to check it against.

Two ledgers, because there were two and nothing summed them: the enrichment log
never saw the agent's calls, and the README used to quote the enrichment log
alone. That is the same shape as the other entries in the failure table — not a
wrong number, a number that did not know about some of the events it claimed to
summarise.

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
                    (+ {{analytics_tests}} unit tests)
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
| Replay harness, staging, SCD2 marts, {{dbt_tests}} dbt tests | **Built and passing** |
| DuckDB + BigQuery dual targets, cost ceilings | **Built** |
| Streamlit dashboard + committed snapshot | **Built** |
| Airflow DAGs + integrity tests | **Executed** — one backfill window end to end, all 7 tasks green; 16 integrity tests |
| `ingestion/load.py` (slice → warehouse) | **Built and proven idempotent on both** — delete-then-insert (DuckDB) / partition-decorator `WRITE_TRUNCATE` (BigQuery) |
| Slice completion markers + torn-load detection | **Built** — verified by killing the loader mid-slice, not by inspection |
| dbt sources reading the *loaded* raw tables | **Wired on BigQuery** — the DuckDB target still reads the CSVs in place, deliberately, for fast credential-free CI |
| Executed BigQuery run | **Done** — 56 windows backfilled, {{dbt_models}} models and {{dbt_tests}} tests green (see *Validation status*) |
| Gemini review enrichment | **Executed** — {{enrichment_texts_labelled_v1}} texts labelled at v{{shipped_prompt_version}} for ${{enrichment_corpus_cost_usd_v1}}, 0 quarantined, labels committed |
| Per-aspect eval + v1→v2 prompt iteration | **Executed** — {{eval_sample_size}}-review sample, micro F1 {{eval_micro_f1_v1}} → {{eval_micro_f1_v2}}; recall reported for 3 of {{eval_aspects_scored_v1}} aspects and withheld for {{eval_recall_not_reported_v1}} |
| `fct_segment_aspect` (RFM × aspect, coverage as a column) | **Built and tested** — complete grid, provenance-stamped, {{dbt_tests}} dbt tests green |
| Gemini demo budget (session/day/lifetime + cached answers) | **Built and tested** — {{analytics_tests}} unit tests including a ten-thread concurrency check |
| Review embeddings (`{{embedding_model}}`, {{vector_search_dimensions}}-d) | **Executed** — all {{review_texts_distinct}} texts, ${{embedding_usd_standard}}, cost log reconciles to zero gap |
| `VECTOR_SEARCH` under the byte ceiling | **Measured on executed queries** — {{vector_search_worst_mib}} MiB, {{vector_search_pct_ceiling}}% of the per-query ceiling |
| NL→SQL agent: parsed SQL guard, injected LIMIT, both ceilings | **Built and evaluated** — {{nl2sql_matched}}/{{nl2sql_gold_total}} execution accuracy ({{nl2sql_accuracy_pct}}%), {{analytics_tests}} unit tests |
| Secret scan over full git history, in CI | **Executed** — clean; verified against a planted key that the scan can fail |

Planned means planned. Nothing in this README describes code that does not
exist.

---

## Dataset licence

Brazilian E-Commerce Public Dataset by Olist, CC BY-NC-SA 4.0. Not
redistributed here — `make data` fetches it from Kaggle.
