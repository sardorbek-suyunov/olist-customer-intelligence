"""
Olist customer intelligence dashboard.

ONE DATA SOURCE, ON PURPOSE
---------------------------
The committed Parquet snapshot in dashboard/data/. No credentials, no warehouse
and no spend, and it keeps working after any key expires.

There used to be a sidebar toggle offering "BigQuery (live)". It was removed,
and the reason is worth keeping. In production the live branch could never run:
it needs GCP_PROJECT_ID, the deployment sets only GEMINI_API_KEY, and a guard
caught that and fell back to the snapshot. So the control was reachable by any
visitor, did nothing, and printed a red error into the sidebar of a portfolio
demo when clicked.

Worse than useless, it was inaccurate. The branch called bigquery.Client() with
Application Default Credentials -- so anywhere it DID run, it ran as the project
owner, while the README described the demo as holding dataViewer on one dataset.
That grant never existed (see the README's IAM note).

Deleting the branch makes a stronger claim than fixing it would: the demo runs
entirely on committed Parquet, and no warehouse credential exists in production
because there is no code path that could use one.

Run locally:  streamlit run dashboard/app.py
"""

from __future__ import annotations

from pathlib import Path

import ask
import duckdb
import pandas as pd
import streamlit as st

DATA = Path(__file__).parent / "data"
TABLES = ["dim_customers", "dim_products", "dim_sellers", "fct_orders"]

st.set_page_config(page_title="Olist Customer Intelligence", page_icon="📦", layout="wide")


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
@st.cache_resource
def snapshot_connection() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the committed Parquet files registered as views."""
    con = duckdb.connect(":memory:")
    for table in TABLES:
        path = (DATA / f"{table}.parquet").as_posix()
        con.execute(f"create view {table} as select * from read_parquet('{path}')")
    return con


@st.cache_data(ttl=600)
def query(sql: str) -> pd.DataFrame:
    return snapshot_connection().execute(sql).df()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("Olist Customer Intelligence")

st.sidebar.caption(
    "**Source: committed Parquet snapshot.** No warehouse credential exists in "
    "this deployment, and no code path could use one."
)

st.sidebar.caption(
    "Coverage 2016-09-04 to 2018-10-17. The source is a static historical "
    "extract; scheduled runs past that date short-circuit by design (ADR 0002)."
)

# ---------------------------------------------------------------------------
# Headline
# ---------------------------------------------------------------------------
st.title("Customer intelligence")

# The headline feature goes first. Everything below it is a chart; this is the
# part that shows the warehouse answering a question nobody wrote a chart for.
ask.render()

st.divider()

totals = query(
    """
    select
        count(*)                        as orders,
        count(distinct customer_unique_id) as customers,
        sum(order_value)                as revenue,
        avg(delivery_days)              as avg_delivery_days
    from fct_orders
    """,
).iloc[0]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Orders", f"{int(totals.orders):,}")
c2.metric("Customers", f"{int(totals.customers):,}")
c3.metric("Revenue", f"R$ {totals.revenue:,.0f}")
c4.metric("Avg delivery", f"{totals.avg_delivery_days:.1f} days")

# ---------------------------------------------------------------------------
# The SCD2 story -- the reason this model exists
# ---------------------------------------------------------------------------
st.header("Why the dimension is Type 2")

st.markdown(
    """
`customer_id` in the Olist source is **order-scoped**: 99,441 rows, 99,441
distinct values, strictly 1:1 with `order_id`. A stock `dbt snapshot` keyed on
it records zero change events. The dimension is therefore derived at
`customer_unique_id` grain from the time-ordered customer rows, joined to
`order_purchase_timestamp`.

`fct_orders` joins to the dimension version **valid at purchase time**, not the
current row. The table below is every order where those two answers differ.
"""
)

drift = query(
    """
    with current_version as (
        select customer_unique_id, customer_city, customer_state, customer_zip_code_prefix
        from dim_customers
        where is_current
    )
    select
        f.order_id,
        f.order_purchase_timestamp,
        d.customer_city  as city_at_purchase,
        d.customer_state as state_at_purchase,
        c.customer_city  as city_today,
        c.customer_state as state_today,
        (d.customer_state <> c.customer_state) as state_differs
    from fct_orders f
    join dim_customers d on d.customer_sk = f.customer_sk
    join current_version c on c.customer_unique_id = d.customer_unique_id
    where d.customer_city  is distinct from c.customer_city
       or d.customer_state is distinct from c.customer_state
       or d.customer_zip_code_prefix is distinct from c.customer_zip_code_prefix
    order by f.order_purchase_timestamp
    """,
)

m1, m2, m3 = st.columns(3)
m1.metric("Orders a naive join mis-attributes", f"{len(drift):,}")
m2.metric("Share of all orders", f"{100 * len(drift) / int(totals.orders):.3f}%")
m3.metric("Wrong at state level", f"{int(drift.state_differs.sum()):,}")

st.caption(
    "Small, but silent: without the as-of join these orders move revenue "
    "between regions in every geographic report."
)
st.dataframe(drift.head(50), width="stretch", hide_index=True)

# ---------------------------------------------------------------------------
# Version distribution
# ---------------------------------------------------------------------------
st.header("Dimension composition")

versions = query(
    """
    select version_number, count(*) as customers
    from dim_customers
    group by version_number
    order by version_number
    """,
)
left, right = st.columns([1, 2])
left.dataframe(versions, hide_index=True, width="stretch")
right.bar_chart(versions.set_index("version_number"), y="customers", height=280)

# ---------------------------------------------------------------------------
# Revenue by state, correctly attributed
# ---------------------------------------------------------------------------
st.header("Revenue by state (attributed at purchase time)")

by_state = query(
    """
    select
        d.customer_state as state,
        count(*)         as orders,
        sum(f.order_value) as revenue
    from fct_orders f
    join dim_customers d on d.customer_sk = f.customer_sk
    where f.order_value is not null
    group by d.customer_state
    order by revenue desc
    limit 15
    """,
)
st.bar_chart(by_state.set_index("state"), y="revenue", height=320)

st.divider()
st.caption(
    "Source: **committed Parquet snapshot** · "
    "All figures reproducible with `make all` · "
    "[Data profiling](docs/data_profiling.md) · [ADR 0001](docs/adr/0001-derive-scd2-customers.md)"
)
