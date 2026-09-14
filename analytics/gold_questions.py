"""
Gold question/SQL pairs for the NL->SQL agent, scored by EXECUTION accuracy.

Every pair here was written by hand, run, and its result inspected before being
used as an answer key. Gold SQL that is subtly wrong makes an eval that reports
a number and measures nothing, which is worse than no eval -- so these are
verified by `scripts/eval_nl2sql.py --validate-gold` before any scoring run, and
that check is part of the harness rather than something remembered.

WHY EXECUTION ACCURACY AND NOT STRING SIMILARITY
------------------------------------------------
There are many correct SQL statements for any of these questions. `count(*)` and
`sum(1)`, a JOIN or a correlated subquery, columns in a different order, a CTE
instead of a nested select -- all identical in result and arbitrarily different
as text. Scoring on text would reward the agent for writing SQL that looks like
mine rather than SQL that answers the question, and would penalise a better
query for being better.

So both statements are executed and their RESULT SETS compared. The comparison
is on values, not column names, because the agent naming a column
`complaint_rate` where the gold says `rate` is not a defect.

THE CATEGORIES ARE DELIBERATE
-----------------------------
`trap` questions are ones where the obvious SQL is wrong in a way that still
returns plausible rows -- the grain of fct_segment_aspect, and the two
denominators. Those are where an agent's failures are most expensive, because
nothing about the output looks wrong. They are scored the same as the rest; the
category exists so the failure analysis can say WHERE it failed, not just how
often.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gold:
    id: str
    question: str
    sql: str
    category: str  # simple | aggregate | join | trap | ranking
    note: str = ""
    # Does the QUESTION ask for an ordering, or does the gold merely have an
    # ORDER BY so its output is stable?
    #
    # Inferring this from the presence of ORDER BY was the first approach and it
    # was wrong in the worst direction: it failed three correct answers. "Which
    # aspects belong to the fulfilment group?" does not ask for an order, so an
    # agent returning the same eight aspects in a different order has answered
    # it. Only questions containing an actual ordering request -- "the five
    # most", "highest first" -- set this.
    ordered: bool = False


GOLD: tuple[Gold, ...] = (
    Gold(
        "g01",
        "How many orders are there in total?",
        "select count(*) as orders from {m}.fct_orders",
        "simple",
    ),
    Gold(
        "g02",
        "How many distinct customers are there?",
        "select count(distinct customer_unique_id) as customers from {m}.fct_orders",
        "simple",
    ),
    Gold(
        "g03",
        "What are the RFM segments?",
        "select distinct rfm_segment from {m}.fct_segment_aspect order by rfm_segment",
        "simple",
    ),
    Gold(
        "g04",
        "How many orders does each segment have?",
        """select distinct rfm_segment, orders_in_segment
           from {m}.fct_segment_aspect order by rfm_segment""",
        "trap",
        "orders_in_segment is segment-level; a plain select repeats it 16 times",
    ),
    Gold(
        "g05",
        "What percentage of each segment's orders have review text?",
        """select distinct rfm_segment, review_text_coverage_pct
           from {m}.fct_segment_aspect order by rfm_segment""",
        "trap",
        "same grain trap as g04",
    ),
    Gold(
        "g06",
        "Which aspect is mentioned in the most orders overall?",
        """select aspect, sum(orders_mentioning_aspect) as total
           from {m}.fct_segment_aspect group by aspect
           order by total desc limit 1""",
        "ranking",
    ),
    Gold(
        "g07",
        "List the five most mentioned aspects with their totals.",
        """select aspect, sum(orders_mentioning_aspect) as total
           from {m}.fct_segment_aspect group by aspect
           order by total desc limit 5""",
        "ranking",
        ordered=True,
    ),
    Gold(
        "g08",
        "Which segment has the highest delivery complaint rate among reviewed orders?",
        """select rfm_segment,
                  sum(case when is_delivery_complaint then aspect_rate_of_reviewed else 0 end) as rate
           from {m}.fct_segment_aspect group by rfm_segment
           order by rate desc limit 1""",
        "trap",
        "must use is_delivery_complaint, and the reviewed denominator",
    ),
    Gold(
        "g09",
        "How many customers are in the champions segment?",
        """select distinct customers_in_segment
           from {m}.fct_segment_aspect where rfm_segment = 'champions'""",
        "trap",
    ),
    Gold(
        "g10",
        "What is the average order value?",
        "select avg(order_value) as avg_order_value from {m}.fct_orders",
        "aggregate",
    ),
    Gold(
        "g11",
        "What is the total revenue across all orders?",
        "select sum(order_value) as revenue from {m}.fct_orders",
        "aggregate",
    ),
    Gold(
        "g12",
        "How many orders were delivered?",
        "select count(*) as delivered from {m}.fct_orders where order_status = 'delivered'",
        "simple",
    ),
    Gold(
        "g13",
        "How many orders are there per order status?",
        """select order_status, count(*) as orders from {m}.fct_orders
           group by order_status order by orders desc""",
        "aggregate",
    ),
    Gold(
        "g14",
        "What is the average delivery time in days for delivered orders?",
        """select avg(delivery_days) as avg_days from {m}.fct_orders
           where order_status = 'delivered' and delivery_days is not null""",
        "aggregate",
    ),
    Gold(
        "g15",
        "Which aspects belong to the fulfilment group?",
        """select distinct aspect from {m}.fct_segment_aspect
           where aspect_group = 'fulfilment' order by aspect""",
        "simple",
    ),
    Gold(
        "g16",
        "How many aspects count as delivery complaints?",
        """select count(distinct aspect) as n from {m}.fct_segment_aspect
           where is_delivery_complaint""",
        "simple",
    ),
    Gold(
        "g17",
        "For the hibernating segment, list aspects by their rate among reviewed orders, highest first.",
        """select aspect, aspect_rate_of_reviewed from {m}.fct_segment_aspect
           where rfm_segment = 'hibernating' order by aspect_rate_of_reviewed desc""",
        "ranking",
        ordered=True,
    ),
    Gold(
        "g18",
        "Which prompt version and model produced the aspect labels?",
        """select distinct label_prompt_version, label_model from {m}.fct_segment_aspect""",
        "simple",
    ),
    Gold(
        "g19",
        "How many orders mention product_quality in each segment?",
        """select rfm_segment, orders_mentioning_aspect from {m}.fct_segment_aspect
           where aspect = 'product_quality' order by rfm_segment""",
        "simple",
    ),
    Gold(
        "g20",
        "What is the highest order value?",
        "select max(order_value) as max_value from {m}.fct_orders",
        "aggregate",
    ),
    Gold(
        "g21",
        "How many orders have more than one seller?",
        "select count(*) as multi_seller from {m}.fct_orders where seller_count > 1",
        "simple",
    ),
    Gold(
        "g22",
        "Compare the champions and hibernating segments on their delivery complaint rate among reviewed orders.",
        """select rfm_segment,
                  sum(case when is_delivery_complaint then aspect_rate_of_reviewed else 0 end) as rate
           from {m}.fct_segment_aspect
           where rfm_segment in ('champions', 'hibernating')
           group by rfm_segment order by rfm_segment""",
        "trap",
    ),
    Gold(
        "g23",
        "How many customers are in each state?",
        """select customer_state, count(*) as customers from {m}.dim_customers
           where is_current group by customer_state order by customers desc""",
        "join",
        "dim_customers is SCD2; a count without is_current double-counts changed customers",
    ),
    Gold(
        "g24",
        "How many product categories are there?",
        "select count(distinct product_category) as categories from {m}.dim_products",
        "simple",
        "the column is product_category, not product_category_name; --validate-gold caught it",
    ),
    Gold(
        "g25",
        "Which segment has the lowest review text coverage?",
        """select rfm_segment, max(review_text_coverage_pct) as coverage
           from {m}.fct_segment_aspect group by rfm_segment
           order by coverage asc limit 1""",
        "trap",
    ),
)

BY_ID: dict[str, Gold] = {g.id: g for g in GOLD}
