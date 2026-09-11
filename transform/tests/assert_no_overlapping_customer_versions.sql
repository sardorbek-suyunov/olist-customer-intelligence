-- No customer may have two versions whose validity intervals overlap.
-- Half-open semantics: an overlap means next.valid_from < current.valid_to.

with windows as (
    select
        customer_unique_id,
        version_number,
        valid_from,
        valid_to,
        lead(valid_from) over (
            partition by customer_unique_id order by version_number
        ) as next_valid_from
    from {{ ref('dim_customers') }}
)

select *
from windows
where next_valid_from is not null
  and next_valid_from < valid_to
