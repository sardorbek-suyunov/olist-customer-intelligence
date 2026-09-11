# ADR 0001 — Derive the customer SCD2 instead of using `dbt snapshot`

**Status:** Accepted · **Date:** 2026-09-11

## Context

`dim_customers` needs Type 2 history so that `fct_orders` can attribute each
order to the customer attributes that were true *at purchase time*.

The idiomatic dbt answer is a snapshot:

```yaml
snapshots:
  - name: customers_snapshot
    config:
      unique_key: customer_id
      strategy: check
      check_cols: [customer_zip_code_prefix, customer_city, customer_state]
```

Profiling the source shows this cannot work:

| Table | Rows | Distinct key | Repeat observations |
|---|---:|---:|---:|
| `olist_customers_dataset` | 99,441 | 99,441 (`customer_id`) | 0 |
| `olist_orders_dataset` | 99,441 | 99,441 (`customer_id`) | 0 |

`customer_id` is an **order-scoped surrogate**: Olist mints a fresh one for
every order, and it is strictly 1:1 with `order_id`. A snapshot keyed on it
produces 99,441 records each with exactly one version and zero change events —
a history table structurally incapable of recording history.

Two further constraints:

1. The person-level key is `customer_unique_id` (96,096 distinct; 2,997 with
   more than one order, max 17). But the customers table carries **no
   timestamp of its own**, so a snapshot has nothing to order versions by.
2. `dbt snapshot` is designed to capture change by repeatedly observing a
   mutating source over wall-clock time. This is a static historical extract.
   There is no "next run" that will observe a different value.

## Decision

Build the dimension as an ordinary incremental model chain rather than a
snapshot:

1. Join each `customer_id` row to its order to borrow
   `order_purchase_timestamp` as the observation time.
2. Re-grain to `customer_unique_id`, ordered by
   `(order_purchase_timestamp, customer_id)`.
3. Hash the normalized location attributes; a new version starts wherever the
   hash differs from the previous row's.
4. Collapse consecutive identical observations, then derive half-open
   `[valid_from, valid_to)` validity intervals.

`dim_products` and `dim_sellers` become **Type 1**. Both have exactly one
observation per key, so no snapshot strategy could ever yield a second version
there regardless of `check_cols`.

## Consequences

**Good.** 259 real change events across 252 customers are captured
(96,096 current + 259 historical = 96,355 rows). The as-of join in `fct_orders`
corrects 272 of 99,441 orders (0.274%) that a naive current-row join
mis-attributes — 43 of them to the wrong *state*.

**Good.** The logic is plain SQL in version control and testable in CI, rather
than state accumulated in a snapshot table that cannot be rebuilt from scratch.

**Cost.** More code than a six-line snapshot config, and two invariants must be
enforced by test rather than by the framework:

- `assert_fct_orders_grain_preserved` — the as-of join stays 1:1. Catches
  fan-out (which a *closed* interval would cause on the 290 tied timestamps)
  and drop-out (which an unfloored version-1 `valid_from` would cause for a
  backdated late arrival).
- `assert_no_overlapping_customer_versions` — intervals never overlap.

**Cost.** Versions can only change where an order exists. A customer who moves
without ordering again is invisible. This is a limit of the source, not of the
model, and it is why `orders_in_version` is exposed on the dimension.

## Alternatives rejected

- **Snapshot on `customer_unique_id`.** No timestamp column to order by, and
  the source is static — `dbt_valid_from` would record the time the pipeline
  ran, not when the customer moved.
- **Type 1 customers.** Simpler, but silently mis-attributes 272 orders and
  makes any geographic trend analysis quietly wrong.
- **Snapshots on products/sellers.** Impossible: one observation per key.
  Shipping empty snapshot configs to look thorough would be worse than not
  having them.
