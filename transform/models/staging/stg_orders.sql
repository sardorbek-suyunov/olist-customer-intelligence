{{ config(materialized='view') }}

-- Grain: one row per order_id. customer_id is 1:1 with order_id.

select
    order_id,
    customer_id,
    order_status,
    cast(order_purchase_timestamp     as {{ dbt.type_timestamp() }}) as order_purchase_timestamp,
    cast(order_approved_at            as {{ dbt.type_timestamp() }}) as order_approved_at,
    cast(order_delivered_carrier_date as {{ dbt.type_timestamp() }}) as order_delivered_carrier_date,
    cast(order_delivered_customer_date as {{ dbt.type_timestamp() }}) as order_delivered_customer_date,
    cast(order_estimated_delivery_date as {{ dbt.type_timestamp() }}) as order_estimated_delivery_date
from {{ source('olist_raw', 'olist_orders_dataset') }}
