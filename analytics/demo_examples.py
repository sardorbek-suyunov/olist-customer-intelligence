"""
The questions the public demo can answer without spending anything.

This is the control that matters most in `gemini_budget`, and it is not a
ceiling. Caps stop a demo dying expensively; they do not stop it being useless
once they trip. These examples make the demo's DEFAULT state free: a visitor who
never clicks "ask your own question" makes no API call and no warehouse scan,
because the answers were computed at build time and committed.

The SQL here is written and reviewed by a human, not generated. That is the
point -- it is the fallback for when the generated path is unavailable, so it
cannot depend on the generated path. Each one is also a worked example of what
good SQL against this schema looks like, which is what the agent's prompt needs
anyway.

Every question deliberately exercises the coverage column, because the single
easiest way to misread this mart is to compare aspect rates across segments
without noticing that the segments are reviewed at different rates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Example:
    id: str
    question: str
    sql: str
    note: str


EXAMPLES: tuple[Example, ...] = (
    Example(
        id="coverage_by_segment",
        question="How many of each segment's orders actually leave review text?",
        sql="""
            select distinct
                rfm_segment,
                customers_in_segment,
                orders_in_segment,
                orders_with_review_text,
                review_text_coverage_pct
            from {marts}.fct_segment_aspect
            order by review_text_coverage_pct desc
        """,
        note=(
            "The question to ask first. Coverage runs from 37% to 44% depending on "
            "the segment, so any aspect rate compared across segments is comparing "
            "over different-sized denominators."
        ),
    ),
    Example(
        id="delivery_complaints_by_segment",
        question="Which customer segment complains most about delivery?",
        sql="""
            select
                rfm_segment,
                max(review_text_coverage_pct) as review_coverage_pct,
                round(sum(case when is_delivery_complaint
                          then aspect_rate_of_reviewed else 0 end), 2)
                    as complaint_rate_among_reviewed,
                round(sum(case when is_delivery_complaint
                          then aspect_rate_of_all_orders else 0 end), 2)
                    as complaint_rate_among_all_orders
            from {marts}.fct_segment_aspect
            group by rfm_segment
            order by complaint_rate_among_reviewed desc
        """,
        note=(
            "Both denominators, side by side. The ranking is the same either way "
            "here, which is worth knowing -- it means the difference is not just "
            "an artefact of who bothers to write a review."
        ),
    ),
    Example(
        id="top_aspects_overall",
        question="What do customers mention most often?",
        sql="""
            select
                aspect,
                aspect_group,
                sum(orders_mentioning_aspect) as orders_mentioning,
                round(100.0 * sum(orders_mentioning_aspect)
                      / sum(orders_with_review_text), 2) as pct_of_reviewed_orders
            from {marts}.fct_segment_aspect
            group by aspect, aspect_group
            order by orders_mentioning desc
        """,
        note=(
            "Praise dominates: delivery_fast and product_quality are the two most "
            "common aspects in the corpus, not complaints."
        ),
    ),
    Example(
        id="champions_vs_hibernating",
        question="Do the best customers complain about different things than lapsed ones?",
        sql="""
            select
                aspect,
                max(case when rfm_segment = 'champions'
                    then aspect_rate_of_reviewed end) as champions_pct,
                max(case when rfm_segment = 'hibernating'
                    then aspect_rate_of_reviewed end) as hibernating_pct
            from {marts}.fct_segment_aspect
            where rfm_segment in ('champions', 'hibernating')
            group by aspect
            order by abs(
                coalesce(max(case when rfm_segment = 'champions'
                         then aspect_rate_of_reviewed end), 0)
                - coalesce(max(case when rfm_segment = 'hibernating'
                           then aspect_rate_of_reviewed end), 0)
            ) desc
        """,
        note="Ordered by the size of the gap, so the aspects that separate them come first.",
    ),
    Example(
        id="repeat_customers",
        question="How many customers ever order twice?",
        sql="""
            select
                orders_per_customer,
                count(*) as customers,
                round(100.0 * count(*) / sum(count(*)) over (), 2) as pct
            from (
                select customer_unique_id, count(*) as orders_per_customer
                from {marts}.fct_orders
                group by customer_unique_id
            ) t
            group by orders_per_customer
            order by orders_per_customer
        """,
        note=(
            "96.88% order exactly once. This is why the mart scores Recency and "
            "Monetary but carries Frequency as a flag: quintiles over a column that "
            "is 1 for nineteen customers in twenty are noise that looks like signal."
        ),
    ),
    Example(
        id="label_provenance",
        question="Which model and prompt version produced these labels?",
        sql="""
            select distinct
                label_model,
                label_prompt_version,
                count(*) over () as rows_in_mart
            from {marts}.fct_segment_aspect
        """,
        note=(
            "One row, by construction. Prompt v2 scored better in the eval and is "
            "deliberately not shipped -- relabelling the corpus costs more than the "
            "remaining budget, and a half-v2 corpus would make aspect frequencies "
            "impossible to attribute."
        ),
    ),
)

BY_ID: dict[str, Example] = {e.id: e for e in EXAMPLES}
