{{ config(materialized='ephemeral') }}

/*
    WHY THIS IS NOT A dbt snapshot
    ------------------------------
    The obvious implementation is `dbt snapshot` on olist_customers_dataset
    with strategy=check. It does not work, and not because of tuning:

      customer_id is unique per row in both customers and orders, and the
      two are strictly 1:1 -- it never repeats, anywhere.

    customer_id is an ORDER-SCOPED surrogate: Olist mints a fresh one per
    order. A snapshot keyed on it emits one record per row, each with exactly
    one version and zero change events -- a history table structurally
    incapable of recording history.

    The person-level key is customer_unique_id. The customers table carries no
    timestamp of its own, so the only way to order a customer's observations is
    to borrow order_purchase_timestamp from the order that minted each
    customer_id.

    Figures deliberately not repeated here -- row counts, distinct counts and
    the change-event totals are measured into docs/figures.json by
    scripts/profile_dataset.py, and a number copied into a comment is a number
    that will eventually disagree with the data.

    The hash is taken over the NORMALIZED attributes so that a future load of
    dirty strings cannot manufacture spurious versions; the raw values are
    carried alongside for display. On the current data the two produce an
    identical set of version boundaries -- asserted by
    assert_normalization_creates_no_spurious_versions.
*/

with observations as (

    select
        c.customer_unique_id,
        c.customer_id,
        o.order_purchase_timestamp as observed_at,
        c.customer_zip_code_prefix,
        c.customer_city,
        c.customer_state,
        c.customer_city_normalized,
        c.customer_state_normalized
    from {{ ref('stg_customers') }} c
    inner join {{ ref('stg_orders') }} o
        on o.customer_id = c.customer_id

),

hashed as (

    select
        *,
        {{ dbt_utils.generate_surrogate_key(['customer_zip_code_prefix', 'customer_city_normalized', 'customer_state_normalized']) }} as attribute_hash
    from observations

),

/*
    Tiebreaker note: 290 (customer_unique_id, order_purchase_timestamp) pairs
    are exact ties, covering 582 rows. None currently differ in address, so no
    zero-length validity window arises -- but without a deterministic
    secondary sort the version numbering would be non-reproducible across
    runs. customer_id breaks the tie.
*/
flagged as (

    select
        *,
        lag(attribute_hash) over (
            partition by customer_unique_id
            order by observed_at, customer_id
        ) as previous_attribute_hash
    from hashed

),

marked as (

    select
        *,
        case
            when previous_attribute_hash is null then 1
            when previous_attribute_hash <> attribute_hash then 1
            else 0
        end as is_version_start
    from flagged

),

numbered as (

    select
        *,
        sum(is_version_start) over (
            partition by customer_unique_id
            order by observed_at, customer_id
            rows between unbounded preceding and current row
        ) as version_number
    from marked

)

-- Collapse consecutive identical observations into one version.
-- The attribute columns are constant within each group by construction.
select
    customer_unique_id,
    version_number,
    min(observed_at)               as version_started_at,
    min(customer_zip_code_prefix)  as customer_zip_code_prefix,
    min(customer_city)             as customer_city,
    min(customer_state)            as customer_state,
    min(customer_city_normalized)  as customer_city_normalized,
    min(customer_state_normalized) as customer_state_normalized,
    min(attribute_hash)            as attribute_hash,
    count(*)                       as observations_in_version
from numbered
group by customer_unique_id, version_number
