{{ config(
    materialized='table',
    partition_by=(
        {'field': 'order_purchase_timestamp', 'data_type': 'timestamp', 'granularity': 'month'}
        if target.type == 'bigquery' else none
    ),
    cluster_by=(['customer_sk', 'order_status'] if target.type == 'bigquery' else none)
) }}

{#
    MONTHLY partitions, not daily. The extract covers 2016-09-04 .. 2018-10-17,
    so daily granularity would create roughly 775 partitions over a table of
    single-digit MB -- far below the ~1 GB partition BigQuery is designed
    around, and partition metadata lookup would dominate the scan it is meant
    to avoid. Monthly gives 26 and still demonstrates pruning on the column
    that every time-bounded query filters on.
#}

/*
    Grain: one row per order_id, and exactly as many rows as the source has
    orders -- asserted by assert_fct_orders_grain_preserved rather than stated
    as a number here.

    THE AS-OF JOIN
    --------------
    The customer dimension is resolved at the version valid at PURCHASE TIME,
    not the customer's current version:

        on  o.order_purchase_timestamp >= d.valid_from
        and o.order_purchase_timestamp <  d.valid_to     -- half-open

    Measured impact of getting this wrong: a small fraction of a percent of
    orders receive attributes from the wrong version, some of them the wrong
    STATE. Small, but it is a silent correctness bug -- it would quietly move
    revenue between regions in every geographic report. The counts live in
    docs/figures.json; see the README for the split between orders on a
    superseded version and orders actually mis-attributed.

    assert_fct_orders_grain_preserved guards the join in both directions:
    a fan-out (duplicate version match) or a drop-out (gap in the validity
    intervals) both change the row count and fail the build.
*/

with orders as (

    select * from {{ ref('stg_orders') }}

),

customer_keys as (

    select customer_id, customer_unique_id
    from {{ ref('stg_customers') }}

),

order_economics as (

    select
        order_id,
        count(*)               as item_count,
        count(distinct seller_id)  as seller_count,
        count(distinct product_id) as product_count,
        sum(item_price)        as items_value,
        sum(freight_value)     as freight_value,
        sum(item_price) + sum(freight_value) as order_value
    from {{ ref('stg_order_items') }}
    group by order_id

)

select
    o.order_id,
    d.customer_sk,
    ck.customer_unique_id,
    o.customer_id as source_customer_id,

    o.order_status,
    o.order_purchase_timestamp,
    o.order_approved_at,
    o.order_delivered_customer_date,
    o.order_estimated_delivery_date,

    coalesce(e.item_count, 0)    as item_count,
    coalesce(e.seller_count, 0)  as seller_count,
    coalesce(e.product_count, 0) as product_count,
    e.items_value,
    e.freight_value,
    e.order_value,

    case
        when o.order_delivered_customer_date is null then null
        else {{ dbt.datediff('o.order_purchase_timestamp', 'o.order_delivered_customer_date', 'day') }}
    end as delivery_days,

    case
        when o.order_delivered_customer_date is null then null
        else {{ dbt.datediff('o.order_estimated_delivery_date', 'o.order_delivered_customer_date', 'day') }}
    end as delivery_days_vs_estimate

from orders o
inner join customer_keys ck
    on ck.customer_id = o.customer_id
inner join {{ ref('dim_customers') }} d
    on  d.customer_unique_id = ck.customer_unique_id
    and o.order_purchase_timestamp >= d.valid_from
    and o.order_purchase_timestamp <  d.valid_to
left join order_economics e
    on e.order_id = o.order_id
