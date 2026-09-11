/*
    The as-of join must be exactly 1:1 with source orders.

    Fails on a FAN-OUT (an order matching two dimension versions, which is what
    a closed [valid_from, valid_to] interval would cause on the 290 tied
    timestamps) and on a DROP-OUT (an order falling in a gap between validity
    intervals, which is what an unfloored version-1 valid_from would cause for
    a backdated late arrival). One assertion, both failure modes.
*/

with counts as (
    select
        (select count(*) from {{ ref('fct_orders') }}) as fact_rows,
        (select count(*) from {{ ref('stg_orders') }}) as source_rows
)

select *
from counts
where fact_rows != source_rows
