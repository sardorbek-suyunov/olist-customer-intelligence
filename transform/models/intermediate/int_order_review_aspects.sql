{{ config(materialized='ephemeral') }}

/*
    Aspects, unpivoted, and attached to orders.

    Grain: one row per (order_id, aspect). Two fan-outs are deliberate and both
    are tested rather than assumed:

    1. A review carries several aspects, so one review becomes several rows.
       That is the point of the model.

    2. A review_id can span more than one order. `review_id` is NOT unique in
       the source -- the true key of stg_order_reviews is (review_id, order_id),
       and 789 review_ids cover more than one order. When that happens the
       review's aspects are attributed to EVERY order it covers, because the
       text is genuinely about all of them and there is no signal in the data
       for splitting it. The consequence is that aspect counts sum to slightly
       more than the number of labelled reviews, which is correct for an
       order-grain question and would be a bug at review grain.

    Orders with no review text simply do not appear here. That absence is the
    whole reason the mart carries coverage as a column: a segment with few
    aspect rows may be a segment with few complaints or a segment nobody
    reviewed, and those are opposite conclusions.
*/

with labelled as (

    select
        review_id,
        label_prompt_version,
        label_model,
        label_aspects_json,
        label_sentiment,
        label_severity,
        label_no_content
    from {{ ref('stg_review_enrichment') }}

),

review_orders as (

    select review_id, order_id
    from {{ ref('stg_order_reviews') }}
    where has_comment_text

),

exploded as (

    select
        l.review_id,
        l.label_prompt_version,
        l.label_model,
        l.label_sentiment,
        l.label_severity,
        l.label_no_content,
        aspect
    from labelled l,
    {{ unnest_json_array('l.label_aspects_json', 'aspect') }}

)

select
    ro.order_id,
    e.review_id,
    e.aspect,
    e.label_prompt_version,
    e.label_model,
    e.label_sentiment,
    e.label_severity,
    e.label_no_content
from exploded e
inner join review_orders ro
    on e.review_id = ro.review_id
