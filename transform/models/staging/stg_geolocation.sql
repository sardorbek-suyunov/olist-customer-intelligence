{{ config(materialized='view') }}

-- Source is ~1M rows, many per zip prefix. Collapsed to one row per prefix
-- using the mean lat/lng *after* clipping to Brazil's bounding box, which
-- removes the handful of points that sit outside the country. (Mean rather
-- than median so the model compiles identically on duckdb and bigquery --
-- bigquery has no aggregate MEDIAN.)

with bounded as (
    select
        cast(geolocation_zip_code_prefix as {{ dbt.type_string() }}) as zip_code_prefix,
        cast(geolocation_lat as {{ dbt.type_float() }}) as lat,
        cast(geolocation_lng as {{ dbt.type_float() }}) as lng,
        {{ normalize_text('geolocation_city') }}  as city,
        {{ normalize_text('geolocation_state') }} as state
    from {{ source('olist_raw', 'olist_geolocation_dataset') }}
    where geolocation_lat between -34.0 and  5.3
      and geolocation_lng between -74.0 and -34.8
)

select
    zip_code_prefix,
    avg(lat) as latitude,
    avg(lng) as longitude,
    min(state)  as state,
    count(*)    as observation_count
from bounded
group by zip_code_prefix
