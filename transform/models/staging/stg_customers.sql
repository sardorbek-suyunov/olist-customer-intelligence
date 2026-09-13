{{ config(materialized='view') }}

/*
    Grain: one row per customer_id (order-scoped), never repeating.
    Deliberately NOT deduplicated to customer_unique_id here -- the
    person-level grain is built in int_customer_address_versions, where the
    order timestamp is available to order a customer's observations.

    Two forms of each location string are carried:
      *_normalized -- used ONLY for change detection hashing
      the plain column -- the raw source value, used for display

    They are not interchangeable. Normalization strips punctuation, which on
    this table rewrites 450 rows across 52 cities that are spelled correctly
    at source (mogi-guacu, santa barbara d'oeste, dias d'avila). Displaying
    the normalized form would mangle real place names for no benefit, so the
    dimension surfaces the raw value and diffs on the normalized one.
*/

select
    customer_id,
    customer_unique_id,
    cast(customer_zip_code_prefix as {{ dbt.type_string() }}) as customer_zip_code_prefix,

    customer_city  as customer_city,
    customer_state as customer_state,

    {{ normalize_text('customer_city') }}  as customer_city_normalized,
    {{ normalize_text('customer_state') }} as customer_state_normalized

from {{ source('olist_raw', 'olist_customers_dataset') }}
