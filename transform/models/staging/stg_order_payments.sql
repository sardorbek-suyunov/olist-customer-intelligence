{{ config(materialized='view') }}

-- Grain: one row per (order_id, payment_sequential).

select
    order_id,
    cast(payment_sequential   as {{ dbt.type_int() }}) as payment_sequential,
    payment_type,
    cast(payment_installments as {{ dbt.type_int() }}) as payment_installments,
    cast(payment_value        as {{ dbt.type_numeric() }}) as payment_value
from {{ source('olist_raw', 'olist_order_payments_dataset') }}
