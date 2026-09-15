# Incident: the BigQuery backfill stopped mid-slice and reported nothing

**Date:** 2026-09-11 (diagnosed 2026-09-13)
**Impact:** 3 orders never loaded; one slice left half-loaded. No incorrect data
served — the marts had not been built yet.
**Status:** Resolved. Structural fix in `b9e7274`.

All times UTC.

## What happened

The first real BigQuery backfill ran to the `2018-10-01` window and stopped
partway through that slice's tables. Nothing reported an error. The run simply
ended, and the warehouse was left holding part of a slice.

```
12:02:16  QUERY  bytesBilledLimitExceeded          (validator, expected)
12:02:38  LOAD   olist_orders_dataset$20160901     FAILED — partition field
...
12:18:26  LOAD   olist_orders_dataset$20181001     DONE
12:18:30  LOAD   olist_customers_dataset$20181001  DONE
          (nothing further — 152 load jobs total)
```

`order_items` is next in `TABLE_MAP` and was never submitted. BigQuery's job
history contains **no failed job at that time**, so nothing was rejected: the
process died client-side. Killed terminal, Ctrl+C, or the machine sleeping — the
evidence does not distinguish them, and it does not need to.

## Impact

| | |
|---|---|
| Windows never attempted | `2018-10-02` … `2018-10-17` — 3 orders |
| Slice left torn | `2018-10-01` — orders + customers loaded, order_payments + order_reviews missing |
| Marts affected | None. `olist` was empty; `dbt build` had never run against BigQuery. |

## Why nothing caught it

A slice is five independent load operations with no transaction spanning them,
and the reconciliation meant to catch a bad load guarded `table in counts`:

```python
if expected is not None and table in counts and counts[table] != expected:
```

A table the loader never reached contributes no entry to `counts`, so it
disagrees with nothing and the check passes. The loader exited 0. Reconstructed
against the exact torn shape, the old check reports zero problems.

A second hole pointed the same way: BigQuery row counts came from
`job.output_rows`, which reports what a job wrote. A second load of the same
slice returns the same number whether the partition was replaced or doubled — it
cannot distinguish the two states it exists to tell apart.

## What was ruled out

`order_items.parquet` for `2018-10-01` is a legitimately empty file — that day
holds one order with no items. It was the obvious suspect and it is not the
cause: the same shape occurs on six days from `2018-09-17` onward, and every
earlier one loaded without complaint. A theory that would have failed five days
sooner does not explain a failure on the sixth.

## Unexplained

The slice files on disk were rewritten between roughly 12:19 and 12:21:31 —
*after* the final load job at 12:18:30 — by a replay pass that issued no loads.
What invoked it is unknown. It is recorded here rather than dropped, because the
honest version is "an anomaly was seen and its blast radius measured", not
silence.

Blast radius is zero, and measured rather than assumed: replay output is
deterministic, and the slice files already tracked in git showed no modification
after that pass. The bytes are identical.

## Fixes

1. **Completeness is recorded, not inferred.** `slice_load_manifest` gets one row
   per slice, written only after every table that slice owes has landed and been
   counted from the warehouse. A torn slice is detectable by absence.
2. **Counts come from the warehouse.** A partition-pruned `COUNT(*)`, not
   `job.output_rows`.
3. **dbt builds behind the gate.** `verify_slice_complete` sits between `load`
   and `dbt_build` in both DAGs, asserted structurally by a DAG integrity test.
4. **Empty slices are marked too.** A day with no orders leaves the tables
   untouched — which is also what an interruption before the first table leaves.
   Without a marker the two are indistinguishable.

The detector was not trusted on inspection. `test_load_completeness` starts the
loader as a subprocess, waits for the third table to be announced, kills the
process, and asserts the marker is absent, `--verify` exits 1, and a retry
repairs to exactly the row counts one clean load produces.

## Repair

Re-running the backfill was the entire repair, because loading is idempotent.
Verified at the time: reloading `20181001` left `olist_orders_dataset` at 99,438
and that partition at exactly 1 row, while `order_payments` and `order_reviews`
each gained the 1 row they had been missing. All five raw tables then reconciled
exactly to source — 99,441 / 99,441 / 112,650 / 103,886 / 99,224.

## Bugs the live BigQuery run exposed

Worth listing, because a first real run against a new engine that finds nothing
usually means nobody looked:

1. `LOAD ... $20160901` — *"The field specified for partitioning cannot be found
   in the schema."* The loader asked BigQuery to partition on `_slice_date`, and
   `replay.py` was not writing that column into the Parquet. Compile-checking
   cannot find this: both halves are individually valid and only disagree when a
   real load job reads a real file. Fixed in `270d18e`, which added
   `SLICE_COLUMN` to the slice writer.
2. `job.output_rows` cannot verify idempotency (above).
3. Torn loads pass reconciliation (above).
4. The `maximum_bytes_billed` ceiling fired for the first time and was observed
   doing so — `bytesBilledLimitExceeded`, *"Query exceeded limit for bytes
   billed: 1048576."* That is the validator's deliberately low **1 MiB** ceiling,
   not the 2 GiB ceiling in `profiles.yml`.
