{{ config(materialized='view') }}

/*
    Grain: one row per seller_id. 3,095 rows, 0 repeat observations -> Type 1.

    Unlike customers, seller_city is genuinely dirty and normalization is
    load-bearing here: 611 raw strings collapse to 603. Real merges include
      'sao paulo' / 'sao  paulo' / 'são paulo'
      "santa barbara d'oeste" / 'santa barbara d´oeste' / 'santa barbara d oeste'
      'rio de janeiro / rio de janeiro' / 'rio de janeiro \rio de janeiro'
    Here the normalized value IS the better display value, because the variants
    are genuine data-entry noise rather than correct alternative spellings.
*/

select
    seller_id,
    cast(seller_zip_code_prefix as {{ dbt.type_string() }}) as seller_zip_code_prefix,
    {{ normalize_text('seller_city') }}  as seller_city,
    {{ normalize_text('seller_state') }} as seller_state,
    seller_city  as seller_city_raw,
    seller_state as seller_state_raw
from {{ source('olist_raw', 'olist_sellers_dataset') }}
