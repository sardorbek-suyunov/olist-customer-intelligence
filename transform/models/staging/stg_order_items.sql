{{ config(materialized='view') }}

-- Grain: one row per (order_id, order_item_id). 112,650 rows.
-- NOTE: price and freight_value live HERE, not on products. A product's
-- selling price is a property of the line item, not of the product record.

select
    order_id,
    cast(order_item_id as {{ dbt.type_int() }}) as order_item_id,
    product_id,
    seller_id,
    cast(shipping_limit_date as {{ dbt.type_timestamp() }}) as shipping_limit_date,
    cast(price         as {{ dbt.type_numeric() }}) as item_price,
    cast(freight_value as {{ dbt.type_numeric() }}) as freight_value
from {{ source('olist_raw', 'olist_order_items_dataset') }}
