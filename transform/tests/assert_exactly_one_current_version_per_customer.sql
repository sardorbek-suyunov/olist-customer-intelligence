select
    customer_unique_id,
    sum(case when is_current then 1 else 0 end) as current_versions
from {{ ref('dim_customers') }}
group by customer_unique_id
having sum(case when is_current then 1 else 0 end) != 1
