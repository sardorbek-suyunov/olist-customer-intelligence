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

**79 dbt tests · 160 Python tests · full build in ~5s on DuckDB · $0 to run**

### ▶ [Try it live](https://olist-customer-intelligence-zwtfxo5cpowpdzk8ycu4ju.streamlit.app)

Ask the warehouse a question in English. The agent writes BigQuery SQL, a parser
refuses anything that is not a single `SELECT` and **injects** the `LIMIT`, and
the query is priced before it runs — 23/25
on a gold set, for $0.01317 of Gemini across the whole evaluation.

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

**[Browse the dbt docs](https://sardorbek.codes/olist-customer-intelligence/)** — every model, column, test and the full lineage graph, generated from the build.

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
| `assert_normalize_macro_matches_python` | The SQL macro and the Python helper agree on all **12,818** distinct location strings. Executed on **both** engines (see *Validation status*). |

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

**A cross-engine claim tested on one engine is not a cross-engine claim.** That
table was true and one model in it had never been compiled for BigQuery at all:
`GCP_PROJECT_ID` was unset, `profiles.yml` defaulted it to `''`, and every
BigQuery command failed with a server message naming nothing. With that fixed,
`fct_segment_aspect` failed immediately on a dialect difference the macro was
supposed to handle — the lateral `UNNEST` alias is `as a(aspect)` on DuckDB and
`as aspect` on BigQuery, and the macro dispatched only the extraction function
because the alias form had been "verified on both" by testing it on DuckDB.

Both engines now build all 14 models and 79 tests. The
default is gone, so a missing project id now says
`Env var required but not provided: 'GCP_PROJECT_ID'`.

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

### Embeddings: executed, and the search verified

All **35,616** distinct texts embedded at
1,536 dimensions. **686,882 tokens,
$0.1374** — against a pre-spend estimate of 686,882 ± 1,260,
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
| bare `VECTOR_SEARCH` | 419.0 MiB | — |
| **full demo shape** (search + text + orders + aspects) | **435.0 MiB** | **42.5%** |

Executed, not estimated. That distinction is load-bearing here: a dry run
approved two queries during this measurement that **could not run at all** — one
against a table whose arrays had silently loaded empty, one with a zero probe
vector. Both times it returned a comfortable number.

**The per-query ceiling is not the constraint. The 5 GiB session budget is:** it
allows only **11 searches per session**. At 3072
dimensions it would be five. The truncation to 1536 was chosen on this
arithmetic before the run, and the measurement confirms it.

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

`23 of 25` gold questions answered correctly —
**92.0% execution accuracy** on `gemini-3.1-flash-lite`, for
$0.01317 of Gemini, on prompt `v2`.
Live at the URL above, behind the caps.

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
| **trap** | **4/6** |

**Every failure is a trap question, and everything else is 19/19.** That is the
result, not the headline percentage. `trap` questions are ones where the obvious
SQL returns plausible rows and the wrong number — mostly the grain of
`fct_segment_aspect`, which has one row per (segment, aspect).

2 remain: `g09`, `g22`. Both are the same
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

### The 2 that remain, and why I did not make them pass

Both are now the *same* shape: the agent returns the **correct values** with an
extra context column. Values below are from the stamped `v2`
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
run; it stays fixed after it. 2 marks is a cheap price
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
| ~~**IAM**~~ | ~~the service account holds `dataViewer` on the marts dataset and nothing else~~ — **this was never true. See below.** |

The three real rows run *inside* the application, so they protect against the
model behaving badly, not against the application behaving badly.

### ⚠️ The IAM row was false, and it was the row called "the boundary"

This table used to end with an IAM grant, described as *"the control that
actually stops a DELETE"*, with the other three presented as defence in depth on
top of it. **It does not exist.** Enumerating the project to write the Terraform
found:

```
$ gcloud projects get-iam-policy olist-customer-intelligence
ROLE         MEMBERS
roles/owner  user:…

$ dataset access entries, all four datasets
projectOwners · projectWriters · projectReaders · owner        ← GCP defaults only
```

No service account, no `dataViewer`, no dataset-level grant. And the live
BigQuery path in `dashboard/app.py` calls `bigquery.Client()` with Application
Default Credentials — so when it runs, it runs **as the project owner**, which is
the opposite of the least privilege the row claimed.

It was always a plan. A note from the setup session reads *"the Streamlit service
account is to be IAM-scoped to marts only **later**"*. The README wrote the
intention in the present tense and then leaned on it, and everything downstream
inherited the error — including the architecture diagram, which labelled the box
*"what actually stops a DELETE"*.

**This is the thirteenth instance**, and its own variety: not a status reported
by the wrong thing, but a control that was described before it was built and
then cited as the foundation of everything above it. The previous twelve were
all found by executing something. This one could not be — there was nothing to
execute. It took enumerating the platform and comparing it to the prose, which
is exactly what the Terraform work forced and nothing else would have.

**The dead code went with the claim.** The dashboard had a sidebar toggle
offering "BigQuery (live)". In production it could never run — it needs
`GCP_PROJECT_ID`, the deployment sets only `GEMINI_API_KEY`, and a guard caught
that and fell back to the snapshot. So it was a control any visitor could click
that did nothing except print a red error into the sidebar. It is removed, which
buys a stronger sentence than fixing it would have:

> **The demo runs entirely on committed Parquet. No warehouse credential exists
> in this deployment, and no code path could use one.**

The record and the dead code were separable, and only one of them was worth
keeping. What limits the BigQuery path — which now only a developer on their own
machine can reach — is `maximum_bytes_billed`, plus the project-level daily
quota that this episode caused to actually get set. See below.

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

## One failure mode, fourteen times

Every bug in this project that survived review shares a shape: **a status
reported by something other than the thing being measured.** Not a wrong answer —
a correct answer to a question nobody meant to ask.

Twelve are in the table below, and every one of those was found by executing
something — each was invisible until then. Two are narrated where they arose
instead, because they break that rule: the thirteenth could not be executed at
all, and the fourteenth is the only one that never became a claim.

| The claim | What was actually measured | How it failed |
|---|---|---|
| `job.output_rows` reports what landed in the partition | Rows *that job wrote*. A second load of the same slice returns the same number whether the partition was replaced or doubled. | The one function whose purpose was verifying idempotency could not distinguish the two states it existed to tell apart. |
| The README reports the test count | A number typed by hand, once, from a report about the data. The README said 68, dbt reports 79, and CI's own comment said 67. | Three sources, three different answers, none of them reading from dbt. |
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

### The fourteenth was caught before it was a claim

Diagnosing a stalled Let's Encrypt certificate on `sardorbek.codes`, one
hypothesis was that GitHub might not be serving the domain over IPv6 — a real
cause of stuck Pages certificates, and invisible to an IPv4 check. The obvious
test is one command:

```console
$ curl -6 -I http://sardorbek.codes/
curl: (7) Failed to connect to 2606:50c0:8000::153 port 80 after 0 ms
```

That is a clean, legible failure, and it would have been written up as *GitHub
is not serving this domain over IPv6*. It measures nothing of the kind. **The
machine running it has no IPv6 route at all** — `curl -6` cannot even resolve a
hostname there, and the connection failed in zero milliseconds because there was
nowhere to send it. The command returns that same output whether or not GitHub
serves the domain, so it cannot tell apart the two cases it was run to tell
apart.

It was caught by running the control first — the same command against a host
known to answer over IPv6, which failed identically. From an external
IPv6-capable vantage the real answer was the opposite of the hypothesis,
`Successfully connected to sardorbek.codes on port 80 over IPv6`, and the cause
was elsewhere: a certificate that had never been requested at all. The API was
not reporting a pending state, it was omitting the field, and those read the same
to a caller that only checks whether the state is `issued`.

This is the ceiling script for the third time — a check that reports the same
thing over a question it is unable to ask. What differs is only the timing. The
other thirteen were found after they had shipped or after they had been believed.
This one was caught in the minute before it was written down, and only because
"run the control first" had by then become a reflex. That is the whole return on
keeping this list.

The fix is identical in every case, and it is not "be more careful":

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
- **Run the control before the test** — a probe that cannot fail is not
  evidence. `curl -6` from a host with no IPv6 route returns one error for "the
  remote is broken" and for "you have no way to ask"; only a known-good target
  separates them. Ask what result would prove the probe itself works, and get
  that result first.

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
| enrichment cost log | $3.5805 | measured, per call |
| demo ledger (NL→SQL + agent eval) | $0.0635 | measured, 132 calls |
| pilots predating the log | $1.56 | **remembered**, from a console reading |
| **total** | **$5.204** | of a $7.39 ceiling |
| remaining | $2.186 | |

**$3.644 of that is measured per call. $1.56 is
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

## Infrastructure as code

`infra/terraform/` describes the GCP side: four BigQuery datasets, four enabled
APIs, and two quota overrides. `terraform validate` and `fmt -check` run in CI.

**Written from an enumeration, not from memory.** Every resource was read out of
the live project first — `gcloud iam service-accounts list`, the BigQuery
client's own dataset and access-entry listing, `gcloud services list --enabled`,
`gcloud alpha services quota list` — then imported into state, then planned until
the plan was empty. That output is committed as
[`infra/terraform/PLAN.txt`](infra/terraform/PLAN.txt):

```
No changes. Your infrastructure matches the configuration.
```

Config that has never been reconciled against the resources it claims to manage
is a description that looks authoritative and was never checked. A `.tf` file is
unusually good at that, because it looks like infrastructure whether or not it
matches any. **The first draft of this one already had the disease**: it gave
each dataset a helpful description, and the first plan reported four in-place
updates, because the live datasets have no description at all. Config that would
have *changed* the project on first apply, while claiming to describe it. The
descriptions are comments now.

**What is deliberately not managed.** The project itself is referenced through a
variable, never managed — a stray `destroy` should not be able to take the
datasets, the billing link and the enrichment output with it. Project IAM is left
alone because its single binding is the owner, and a botched apply that removes
the only administrator is worse than anything it prevents. The
`ais-gemini-key-…` service account is Google-created and Google-managed.

**The data is protected twice.** `prevent_destroy` stops Terraform planning a
destroy at all; `delete_contents_on_destroy = false` means BigQuery then refuses
to drop a non-empty dataset. There is no `deletion_protection` argument on
`google_bigquery_dataset` — it exists on tables, not datasets — so that is the
whole of what is available, and it is stated rather than implied. `olist_raw` and
`olist_marts` hold enrichment output that cost $3.19
and 85 minutes and cannot be regenerated for free.

### The quota that was claimed for weeks and did not exist

The README asserted a 10 GiB/day BigQuery ceiling — platform-enforced rather than
application-enforced, for the same reason this project prefers
`maximum_bytes_billed` to asking a model nicely. Enumerating found
`bigquery.googleapis.com/quota/query/usage` reporting `consumerOverride: null`.

**It did not exist.** What did were two overrides, both at exactly 10,000,000,000,
neither constraining query scanning:

| metric | constrains | reality |
|---|---|---|
| `quota/extract/bytes` | bytes extract jobs write | this project runs none |
| `…alloydb_federated_query_cross_region_bytes` | AlloyDB federated queries | there is no AlloyDB |

Both names contain "bytes" and sit near the query metrics in a filtered console
list. It is now set correctly, and the claim is true:

```
$ gcloud alpha services quota list --service=bigquery.googleapis.com …
  quota/query/usage   default=209715200 MiB   effective=10240 MiB  (= 10 GiB/day)
```

**The unit nearly caused a second silent failure.** The first draft of
`quotas.tf` said `10 GiB/day = 10737418240` — the figure in *bytes*. The Service
Usage API expresses this metric in **mebibytes**: the documented default is
200 TiB and the API reports `209715200`, and 209715200 MiB is exactly 200 TiB,
which is what pins it down. Applying the bytes figure would have set ~10 PiB/day
— **no limit at all, reading in the config like a tight one, and reporting
success.** The console shows the same number in TiB, a third unit.

The AlloyDB override was deleted: it constrained a product this project does not
use, so there is no reading under which it was a control. `quota/extract/bytes`
was **kept, and thereby converted from an accident into a decision** — an extract
job is the one real egress path out of the warehouse, nothing here runs one, and
10 GB against a ~120 MB dataset cannot bite a legitimate use while capping a bad
one. Keeping an accident because it turned out useful is a bad habit; writing
down why you are keeping it is not.

### The budget alert exists, and now that is checked rather than believed

It could not be verified before, because `billingbudgets.googleapis.com` was not
enabled and listing budgets without it fails with a permission error that reads
like a missing grant. So it sat in the same category as the IAM row: an asserted
control nothing had confirmed.

It was exactly as described — $5 monthly, thresholds at 50/90/100/150%. **Two of
the three asserted controls in this project turned out to be false; this was the
one that was true.** That ratio is the argument for checking, not against it.

One correction: its filter carries no `projects` entry, so it covers the whole
**billing account**, not this project. Identical numbers today, different numbers
the moment a second project appears under it. And a budget notifies — it does not
cap. Nothing about crossing $5 stops a query, which is exactly why the quota
above matters.

### CI validates, it does not plan

A `plan` needs credentials against a live project. This pipeline is deliberately
credential-free — every other job runs against DuckDB reading CSVs in place, with
no cloud identity anywhere — and putting a long-lived service-account key in
repository secrets to earn a nicer badge would trade a real property of the
pipeline for the appearance of rigour.

**Workload Identity Federation would close that gap properly**, letting the job
mint a short-lived token from GitHub's OIDC identity with no stored key at all.
It is not configured here. That is a **deliberate not-done, not an unknown**: it
needs a workload identity pool, a provider bound to this repository, and a
service account with the right impersonation binding — real setup, worth doing
when something actually depends on CI planning, and dishonest to imply is
present. The local plan output is committed instead, which a reader can check.

---

## Repository layout

```
analytics/          NL->SQL spend ceilings, Gemini demo budget, cached examples
                    (+ 103 unit tests)
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
| Gemini demo budget (session/day/lifetime + cached answers) | **Built and tested** — 103 unit tests including a ten-thread concurrency check |
| Review embeddings (`gemini-embedding-2`, 1,536-d) | **Executed** — all 35,616 texts, $0.1374, cost log reconciles to zero gap |
| `VECTOR_SEARCH` under the byte ceiling | **Measured on executed queries** — 435.0 MiB, 42.5% of the per-query ceiling |
| NL→SQL agent: parsed SQL guard, injected LIMIT, both ceilings | **Built and evaluated** — 23/25 execution accuracy (92.0%), 103 unit tests |
| Secret scan over full git history, in CI | **Executed** — clean; verified against a planted key that the scan can fail |

Planned means planned. Nothing in this README describes code that does not
exist.

---

## Dataset licence

Brazilian E-Commerce Public Dataset by Olist, CC BY-NC-SA 4.0. Not
redistributed here — `make data` fetches it from Kaggle.
