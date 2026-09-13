{{ config(materialized='table') }}

/*
    TYPE 1 -- one observation per seller_id, and no repeats anywhere.

    This is where text normalization actually earns its place: 611 raw
    seller_city strings collapse to 603 canonical ones. Both the raw and the
    normalized value are kept so the cleaning is auditable rather than lossy.
*/

select
    s.seller_id,
    s.seller_zip_code_prefix,
    s.seller_city,
    s.seller_state,
    s.seller_city_raw,
    (s.seller_city_raw <> s.seller_city) as seller_city_was_normalized,
    g.latitude  as seller_latitude,
    g.longitude as seller_longitude
from {{ ref('stg_sellers') }} s
left join {{ ref('stg_geolocation') }} g
    on g.zip_code_prefix = s.seller_zip_code_prefix
