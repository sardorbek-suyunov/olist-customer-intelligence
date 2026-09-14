"""
The "ask the data" panel: cached answers first, live NL->SQL second.

THE DEFAULT STATE IS FREE
-------------------------
Six worked questions ship with their SQL and their answers, computed at build
time by scripts/export_demo_examples.py. They render with no API call, no
warehouse and no credential, so a visitor who never types anything costs
nothing and still sees the thing working -- including the SQL, which is most of
what makes a SQL demo worth looking at.

Live questions are the bonus path, and when a ceiling trips the panel degrades
to the cached set with a sentence saying which ceiling and when it resets. Caps
stop a demo dying expensively; they do not stop it being useless once they trip,
and a demo that is useless the moment it is popular is the failure this whole
budget exists to avoid.

WHAT THE VISITOR IS SHOWN ABOUT THE AGENT
-----------------------------------------
The generated SQL, always, next to the answer. Partly because it is the
interesting artefact, and partly because it is the honest one: a table of
numbers from a language model is a claim, and the query is the evidence for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

DATA = Path(__file__).parent / "data"
CACHED = DATA / "demo_examples.json"


def _bridge_secrets() -> None:
    """
    Streamlit secrets -> os.environ, once.

    `analytics.settings` and `enrichment.client` both read os.environ, because
    they are used from the CLI, from CI and from pytest where `st.secrets` does
    not exist. Streamlit does NOT export secrets to the environment on its own,
    so without this bridge every ceiling on the deployed app silently falls back
    to its default and the API key is simply absent -- the live path would be
    permanently "off", with no error to explain why.

    Existing environment variables win, so a local .env or a shell export is not
    overridden by a secrets file.
    """
    import os

    try:
        secrets = dict(st.secrets)
    except Exception:  # noqa: BLE001 - no secrets file is the normal local case
        secrets = {}
    for key, value in secrets.items():
        if isinstance(value, str | int | float) and key not in os.environ:
            os.environ[key] = str(value)

    # Local convenience: the same .env the CLI uses, read with the decoder that
    # copes with the UTF-16 file PowerShell writes.
    try:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from enrichment.client import load_env

        load_env()
    except Exception:  # noqa: BLE001 - absent .env is normal in deployment
        pass


_bridge_secrets()


@st.cache_data
def cached_examples() -> list[dict]:
    if not CACHED.exists():
        return []
    return json.loads(CACHED.read_text(encoding="utf-8"))["examples"]


@st.cache_resource
def snapshot_runner():
    """One DuckDB connection over the committed Parquet, shared by all sessions."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analytics.snapshot_runner import SnapshotRunner

    return SnapshotRunner()


def _agent(model: str):
    """
    Built per session, because the Gemini budget is per session.

    Not cached: `DemoBudget` counts this visitor's calls, and a cached agent
    would hand every visitor the same allowance object -- which is the same
    class of bug as the per-instance lock the concurrency test caught.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analytics.nl2sql import Agent

    return Agent(runner=snapshot_runner(), model=model)


def render(model: str = "gemini-3.1-flash-lite") -> None:
    st.header("Ask the data")

    examples = cached_examples()
    if not examples:
        st.info("No cached answers yet — run `make demo-examples`.")
        return

    st.caption(
        "Questions answered in SQL. The six below were computed at build time and "
        "cost nothing to show; **ask your own** and a model writes the SQL, which "
        "is parsed, given a LIMIT, and run against the committed snapshot."
    )

    # ---------------------------------------------------------------- cached
    labels = [e["question"] for e in examples]
    picked = st.selectbox("Worked examples", labels, index=0)
    example = next(e for e in examples if e["question"] == picked)

    st.dataframe(pd.DataFrame(example["rows"]), width="stretch", hide_index=True)
    if example.get("note"):
        st.caption(example["note"])
    with st.expander("SQL"):
        st.code(example["sql"], language="sql")

    st.divider()

    # ------------------------------------------------------------------ live
    try:
        agent = _agent(model)
    except Exception as exc:  # noqa: BLE001 - a missing key is a normal state here
        st.info(
            "Live questions are off on this deployment — no API key is configured. "
            "The examples above are precomputed and always work."
        )
        st.caption(f"({type(exc).__name__})")
        return

    state = agent.budget.state()
    if not state["live_questions_available"]:
        st.warning(
            "Live questions are paused: the demo has used its budget for today. "
            "It resets tomorrow, and the examples above are precomputed so they "
            "always work."
        )
        return

    st.markdown("**Ask your own**")
    question = st.text_input(
        "Question",
        placeholder="e.g. which segment complains most about delivery?",
        label_visibility="collapsed",
    )
    st.caption(
        f"{state['session_calls_left']} questions left this session · "
        f"${state['day_usd_left']:.3f} of today's budget · "
        f"${state['lifetime_usd_left']:.2f} remaining overall"
    )

    if not question:
        return

    from analytics.gemini_budget import DemoBudgetExceeded
    from analytics.sql_guard import UnsafeSQL

    try:
        with st.spinner("Writing SQL…"):
            answer = agent.ask(question)
    except DemoBudgetExceeded as exc:
        # Named ceiling, and what to do instead. Not a stack trace.
        st.warning(str(exc))
        return
    except UnsafeSQL as exc:
        st.error(f"That query was refused before it ran: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - the message is the useful part
        st.error(f"The generated SQL did not run: {str(exc)[:300]}")
        return

    if not answer.rows:
        st.info("That query returned no rows.")
    else:
        st.dataframe(pd.DataFrame(answer.rows), width="stretch", hide_index=True)

    st.code(answer.sql, language="sql")
    st.caption(
        f"${answer.usd_spent:.5f} of Gemini · "
        + ("LIMIT added by the guard · " if answer.limit_added else "")
        + f"tables: {', '.join(answer.tables)}"
    )
