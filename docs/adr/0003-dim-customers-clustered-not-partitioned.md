# ADR 0003: `dim_customers` is clustered, not partitioned

**Status:** Accepted
**Date:** 2026-09-13
**Supersedes:** nothing. Corrects the partition config shipped before the
BigQuery path had ever been executed.

## Context

`dim_customers` is a Type 2 dimension whose validity intervals are half-open
`[valid_from, valid_to)`. Version 1 of every customer has to be valid from the
beginning of time, because an order can arrive with a purchase timestamp earlier
than any observation that minted a version — a backdated late arrival that must
still find exactly one row to join to. The sentinel for "the beginning of time"
is **1900-01-01**, floored deliberately so that
`assert_fct_orders_grain_preserved` cannot silently drop such an order.

The table was originally configured with BigQuery time-unit partitioning on
`valid_from`, by symmetry with `fct_orders`. That configuration had never been
executed — the BigQuery target was compile-checked only — and it cannot work.

**BigQuery time-unit partitioning only accepts values in 1960-01-01 … 2159-12-31.**
A partitioned table rejects rows outside that range outright. Of 96,355 rows in
the dimension, 96,096 are version 1 and therefore carry `valid_from = 1900-01-01`
— 99.7% of the table, every one of them illegal. The build would have failed on
its first real BigQuery run.

## Decision

Drop partitioning from `dim_customers`. Keep clustering.

`fct_orders` stays partitioned, but moved from daily to **monthly** for an
unrelated sizing reason: the extract covers 2016-09-04 … 2018-10-17, so daily
partitioning yields 775 partitions averaging ~13 KB, far below the ~1 GB
partition BigQuery is designed around.

## Considered and rejected: move the sentinel

The obvious alternative is to floor version 1 at 1960-01-01 instead of
1900-01-01, which is inside the legal range and would let the partitioning stand.

Rejected, because it buys nothing and costs correctness headroom:

1. **Partitioning is the wrong tool at this size.** `dim_customers` is ~4.5 MB.
   BigQuery prunes partitions to avoid scanning gigabytes; there are no gigabytes
   here. A full scan of the entire dimension is already far below the 1 GiB
   per-query ceiling in `analytics/bq_safety.py`, let alone the 2 GiB job ceiling
   in `profiles.yml`. The optimisation being preserved does not exist.

2. **The sentinel is a correctness device, not a date.** Its only job is to be
   earlier than any conceivable `order_purchase_timestamp`. 1900-01-01 has
   obvious slack; 1960-01-01 has slack only until someone loads a dataset with a
   1950s timestamp in it. Choosing a sentinel to satisfy a storage feature the
   table does not benefit from inverts the priority — it makes a silent
   correctness property contingent on a performance decision.

3. **The failure modes are asymmetric.** A too-early sentinel costs nothing. A
   sentinel that stops being early enough drops rows from the as-of join, which
   `assert_fct_orders_grain_preserved` would catch — but only after someone had
   to debug why a dimension's storage config was dictating its semantics.

Clustering on the join keys delivers what actually matters here: the as-of join
and the `is_current` filter both benefit, with no constraint on column values.

## Consequences

- `dim_customers` has no partition pruning. At 4.5 MB this is not measurable.
- The 1900-01-01 sentinel stays, and stays load-bearing. It is asserted by
  `assert_fct_orders_grain_preserved`, which catches drop-out from an unfloored
  version-1 `valid_from` and fan-out from a closed interval in one test.
- `fct_orders` keeps monthly partitions, which are legal for all its values since
  every `order_purchase_timestamp` falls inside the coverage window.
- Anyone re-adding partitioning to this model will fail on the first BigQuery
  build rather than in review, which is the right place for it to fail.
