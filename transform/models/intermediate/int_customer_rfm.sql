{{ config(materialized='ephemeral') }}

/*
    RFM per customer -- with the F deliberately not scored.

    THE MEASUREMENT THAT SHAPED THIS MODEL
    --------------------------------------
    Textbook RFM quintiles all three dimensions. On this dataset that produces a
    meaningless Frequency score, because 96.88% of customers have exactly one
    order:

        orders per customer     customers      share
        1                          93,099     96.88%
        2                           2,745      2.86%
        3+                            252      0.26%

    NTILE(5) over a column that is 1 for nineteen customers in twenty does not
    find five groups. It cuts the ties into five arbitrary blocks, and which
    block a customer lands in is decided by the sort's tiebreak rather than by
    anything about the customer. The score would look exactly like a real one.

    So Frequency is carried as what it actually is -- a count and a repeat flag
    -- and the segmentation is built on R and M, which are genuinely continuous.
    An honest two-dimensional segmentation beats a three-dimensional one whose
    third dimension is noise.

    RECENCY IS RELATIVE TO THE EXTRACT, NOT TO TODAY
    ------------------------------------------------
    The dataset ends 2018-10-17 and is static. Measuring recency against
    current_date would make every customer look equally ancient, and the whole R
    dimension would collapse to a constant that drifts by one each night. The
    anchor is the latest purchase in the data, so "recent" means recent as of the
    extract -- which is the only thing this data can support. Same reasoning as
    ADR 0002 on the schedule.
*/

with orders as (

    select
        customer_unique_id,
        order_id,
        order_purchase_timestamp,
        coalesce(order_value, 0) as order_value
    from {{ ref('fct_orders') }}

),

anchor as (

    select max(order_purchase_timestamp) as as_of from orders

),

per_customer as (

    select
        o.customer_unique_id,
        count(*)                          as order_count,
        sum(o.order_value)                as monetary_value,
        max(o.order_purchase_timestamp)   as last_order_at,
        {{ dbt.datediff('max(o.order_purchase_timestamp)', 'a.as_of', 'day') }} as recency_days
    from orders o
    cross join anchor a
    group by o.customer_unique_id, a.as_of

),

scored as (

    select
        customer_unique_id,
        order_count,
        monetary_value,
        last_order_at,
        recency_days,
        (order_count > 1) as is_repeat_customer,
        -- Fewer days since the last order is better, so the quintile is
        -- descending: score 5 is the most recent fifth.
        ntile(5) over (order by recency_days desc) as r_score,
        ntile(5) over (order by monetary_value asc) as m_score
    from per_customer

)

select
    customer_unique_id,
    order_count,
    is_repeat_customer,
    monetary_value,
    last_order_at,
    recency_days,
    r_score,
    m_score,
    case
        when r_score >= 4 and m_score >= 4 then 'champions'
        when r_score <= 2 and m_score >= 4 then 'high_value_lapsed'
        when r_score >= 4 and m_score <= 2 then 'recent_low_value'
        when r_score <= 2 and m_score <= 2 then 'hibernating'
        when r_score >= 3 and m_score >= 3 then 'loyal'
        else 'needs_attention'
    end as rfm_segment
from scored
