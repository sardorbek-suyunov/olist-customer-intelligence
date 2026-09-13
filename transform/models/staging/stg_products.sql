{{ config(materialized='view') }}

-- Grain: one row per product_id, with no repeat observations.
-- There is exactly one observation per key and no price column, so this
-- dimension has no derivable change history. Type 1 by necessity.

select
    p.product_id,
    p.product_category_name,
    coalesce(t.product_category_name_english, 'unknown') as product_category,
    cast(p.product_name_lenght        as {{ dbt.type_int() }}) as product_name_length,
    cast(p.product_description_lenght as {{ dbt.type_int() }}) as product_description_length,
    cast(p.product_photos_qty         as {{ dbt.type_int() }}) as product_photos_qty,
    cast(p.product_weight_g           as {{ dbt.type_int() }}) as product_weight_g,
    cast(p.product_length_cm          as {{ dbt.type_int() }}) as product_length_cm,
    cast(p.product_height_cm          as {{ dbt.type_int() }}) as product_height_cm,
    cast(p.product_width_cm           as {{ dbt.type_int() }}) as product_width_cm
from {{ source('olist_raw', 'olist_products_dataset') }} p
left join {{ source('olist_raw', 'product_category_name_translation') }} t
    on t.product_category_name = p.product_category_name
