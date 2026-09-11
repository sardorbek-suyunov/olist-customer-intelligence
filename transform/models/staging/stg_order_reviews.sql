{{ config(materialized='view') }}

-- Grain: one row per review_id... almost. review_id repeats for a small number
-- of rows (one review spanning multiple orders), so the true key is
-- (review_id, order_id). Tested in _staging.yml.

select
    review_id,
    order_id,
    cast(review_score as {{ dbt.type_int() }}) as review_score,
    nullif(trim(review_comment_title), '')   as review_comment_title,
    nullif(trim(review_comment_message), '') as review_comment_message,
    (nullif(trim(review_comment_message), '') is not null) as has_comment_text,
    cast(review_creation_date   as {{ dbt.type_timestamp() }}) as review_creation_date,
    cast(review_answer_timestamp as {{ dbt.type_timestamp() }}) as review_answer_timestamp
from {{ source('olist_raw', 'olist_order_reviews_dataset') }}
