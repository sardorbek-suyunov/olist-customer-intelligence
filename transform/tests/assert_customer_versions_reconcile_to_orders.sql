-- Every order must contribute to exactly one dimension version observation.
-- Guards the customers-to-orders join in int_customer_address_versions.

with expected as (
    select count(*) as n from {{ ref('stg_orders') }}
),
actual as (
    select sum(orders_in_version) as n from {{ ref('dim_customers') }}
)

select expected.n as expected_orders, actual.n as observed_orders
from expected cross join actual
where expected.n != actual.n
