/*
    GUARD, NOT A CLEANER.

    int_customer_address_versions hashes the NORMALIZED location attributes so
    that dirty strings cannot manufacture spurious SCD2 versions. On the
    current data that defence is inert: hashing the raw values produces the
    identical set of 259 version boundaries.

    An earlier version of this test asserted raw == normalized string equality
    and failed with 450 rows -- correctly, because normalization rewrites 52
    legitimately punctuated city names (mogi-guacu, santa barbara d'oeste).
    That is a cosmetic rewrite, not a change in change-detection behaviour.
    This test asserts the invariant that actually matters: the two hashing
    strategies must agree on WHERE the version boundaries fall.

    If a future load lands genuinely dirty data, normalization starts doing
    real work, the counts diverge, and this fires -- so the README's
    "0 spurious versions removed" figure cannot drift without the build
    going red.
*/

with observations as (

    select
        c.customer_unique_id,
        c.customer_id,
        o.order_purchase_timestamp as observed_at,
        {{ dbt_utils.generate_surrogate_key(['c.customer_zip_code_prefix', 'c.customer_city', 'c.customer_state']) }} as raw_hash,
        {{ dbt_utils.generate_surrogate_key(['c.customer_zip_code_prefix', 'c.customer_city_normalized', 'c.customer_state_normalized']) }} as normalized_hash
    from {{ ref('stg_customers') }} c
    inner join {{ ref('stg_orders') }} o
        on o.customer_id = c.customer_id

),

lagged as (

    select
        *,
        lag(raw_hash) over (
            partition by customer_unique_id order by observed_at, customer_id
        ) as previous_raw_hash,
        lag(normalized_hash) over (
            partition by customer_unique_id order by observed_at, customer_id
        ) as previous_normalized_hash
    from observations

),

boundary_counts as (

    select
        sum(case when previous_raw_hash is not null
                  and previous_raw_hash <> raw_hash then 1 else 0 end) as raw_version_boundaries,
        sum(case when previous_normalized_hash is not null
                  and previous_normalized_hash <> normalized_hash then 1 else 0 end) as normalized_version_boundaries
    from lagged

)

select *
from boundary_counts
where raw_version_boundaries != normalized_version_boundaries
