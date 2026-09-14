{{ config(materialized='table') }}

/*
    RFM segment x review aspect.

    Grain: one row per (rfm_segment, aspect), complete. Every segment carries a
    row for every aspect in the taxonomy, zero-filled, because a missing row and
    a zero are different claims and a chart cannot tell them apart.

    COVERAGE IS A COLUMN, NOT A FOOTNOTE
    ------------------------------------
    Only ~41% of orders carry review text. So every aspect rate here is computed
    over a minority of orders, and the minority is not the same size in every
    segment. A segment showing few delivery complaints might have few
    complaints, or might be a segment that does not write reviews -- opposite
    conclusions from the same number.

    The mart therefore refuses to publish a single rate. It carries:

      review_text_coverage_pct    what share of the segment's orders can speak
      aspect_rate_of_reviewed     rate among orders that have review text
      aspect_rate_of_all_orders   the same numerator over ALL orders

    The second is the one people want and the third is the one that is safe to
    compare across segments. Publishing only the second would invite exactly the
    mistake the coverage column exists to prevent, and publishing only the third
    understates every aspect by the coverage factor. Both, plus the coverage, is
    the only combination that cannot be read wrongly by accident.

    PROVENANCE IS STAMPED ON EVERY ROW
    ----------------------------------
    label_prompt_version and label_model travel with the data. The eval in the
    README reports that prompt v2 scored better than v1; the corpus ships v1,
    because relabelling it costs more than the remaining budget and a half-v2
    corpus would make aspect frequencies unattributable. A reader looking at
    these numbers should be able to tell which prompt produced them from the row
    itself, not from remembering a paragraph. See docs/DECISIONS.md 5b.
*/

with customers as (

    select * from {{ ref('int_customer_rfm') }}

),

orders as (

    select
        o.order_id,
        o.customer_unique_id,
        r.has_review_text
    from {{ ref('fct_orders') }} o
    left join (
        select order_id, max(case when has_comment_text then 1 else 0 end) = 1 as has_review_text
        from {{ ref('stg_order_reviews') }}
        group by order_id
    ) r on o.order_id = r.order_id

),

segment_totals as (

    select
        c.rfm_segment,
        count(distinct c.customer_unique_id) as customers_in_segment,
        count(o.order_id)                    as orders_in_segment,
        sum(case when coalesce(o.has_review_text, false) then 1 else 0 end)
            as orders_with_review_text
    from customers c
    inner join orders o
        on c.customer_unique_id = o.customer_unique_id
    group by c.rfm_segment

),

-- The complete grid. Cross join before counting, so an aspect a segment never
-- mentions lands as 0 rather than vanishing.
grid as (

    select
        s.rfm_segment,
        t.aspect,
        t.aspect_group,
        t.is_delivery_complaint
    from (select distinct rfm_segment from segment_totals) s
    cross join {{ ref('aspect_taxonomy') }} t

),

mentions as (

    select
        c.rfm_segment,
        a.aspect,
        count(distinct a.order_id)  as orders_mentioning_aspect,
        count(distinct a.review_id) as reviews_mentioning_aspect,
        max(a.label_prompt_version) as label_prompt_version,
        max(a.label_model)          as label_model
    from {{ ref('int_order_review_aspects') }} a
    inner join orders o
        on a.order_id = o.order_id
    inner join customers c
        on o.customer_unique_id = c.customer_unique_id
    group by c.rfm_segment, a.aspect

)

select
    g.rfm_segment,
    g.aspect,
    g.aspect_group,
    g.is_delivery_complaint,

    -- Provenance, on every row.
    '{{ var("shipped_prompt_version") }}' as label_prompt_version,
    '{{ var("shipped_label_model") }}'    as label_model,

    t.customers_in_segment,
    t.orders_in_segment,
    t.orders_with_review_text,

    -- Coverage. The denominator problem, made impossible to miss.
    case
        when t.orders_in_segment = 0 then 0
        else round(100.0 * t.orders_with_review_text / t.orders_in_segment, 2)
    end as review_text_coverage_pct,

    coalesce(m.orders_mentioning_aspect, 0)  as orders_mentioning_aspect,
    coalesce(m.reviews_mentioning_aspect, 0) as reviews_mentioning_aspect,

    case
        when t.orders_with_review_text = 0 then null
        else round(
            100.0 * coalesce(m.orders_mentioning_aspect, 0) / t.orders_with_review_text, 4
        )
    end as aspect_rate_of_reviewed,

    case
        when t.orders_in_segment = 0 then null
        else round(100.0 * coalesce(m.orders_mentioning_aspect, 0) / t.orders_in_segment, 4)
    end as aspect_rate_of_all_orders

from grid g
inner join segment_totals t
    on g.rfm_segment = t.rfm_segment
left join mentions m
    on g.rfm_segment = m.rfm_segment
    and g.aspect = m.aspect
