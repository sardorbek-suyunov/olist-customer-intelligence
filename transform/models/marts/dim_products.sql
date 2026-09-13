{{ config(materialized='table') }}

/*
    TYPE 1 -- by necessity, not by choice.

    olist_products_dataset has exactly one row per product_id:
    exactly one observation per key, so there is nothing to diff and no
    snapshot strategy can ever produce a second version.

    An earlier draft of this project justified an SCD2 here on the grounds
    that "price changes over time". That was wrong -- there is no price column
    on products at all. price and freight_value are attributes of the order
    line (see stg_order_items), which is the correct modelling anyway: a
    selling price is a property of a transaction, not of a product record.
*/

select
    product_id,
    product_category,
    product_category_name as product_category_name_pt,
    product_weight_g,
    product_length_cm,
    product_height_cm,
    product_width_cm,
    product_photos_qty,
    product_name_length,
    product_description_length,
    case
        when product_weight_g is null or product_length_cm is null then null
        else product_length_cm * product_height_cm * product_width_cm
    end as product_volume_cm3
from {{ ref('stg_products') }}
