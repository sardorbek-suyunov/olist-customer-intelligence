"""
The parts of the agent that do not need a network: prompt assembly, fence
stripping, and the settings that let a ceiling be raised without a commit.
"""

from __future__ import annotations

import pytest

from analytics import settings
from analytics.gold_questions import GOLD
from analytics.nl2sql import build_prompt, example_prompt, strip_fences
from analytics.settings import BadSetting


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("select 1 from t", "select 1 from t"),
        ("```sql\nselect 1 from t\n```", "select 1 from t"),
        ("```\nselect 1 from t\n```", "select 1 from t"),
        ("```SQL\nselect 1 from t```", "select 1 from t"),
        ("  select 1 from t  ", "select 1 from t"),
    ],
)
def test_fences_are_stripped_rather_than_rejected(raw: str, expected: str) -> None:
    """
    Models fence their SQL however firmly the prompt asks them not to. Rejecting
    the answer would spend a retry, and a slice of the visitor's budget, to
    punish a formatting habit.
    """
    assert strip_fences(raw) == expected


def test_the_prompt_carries_the_marts_and_their_column_notes() -> None:
    """
    The schema comes from the dbt manifest, so this also asserts the manifest is
    built. A prompt missing the coverage column's description is a prompt that
    invites the exact denominator mistake the mart exists to prevent.
    """
    prompt = build_prompt("how many orders are there?")
    assert "olist_marts.fct_segment_aspect" in prompt
    assert "aspect_rate_of_reviewed" in prompt
    assert "review_text_coverage_pct" in prompt
    assert "how many orders are there?" in prompt


def test_the_examples_are_the_demo_s_own_reviewed_sql() -> None:
    """Few-shot examples and the zero-cost fallback are the same six queries."""
    examples = example_prompt()
    assert "coverage_by_segment" not in examples  # ids are not in the prompt
    assert examples.count("Q: ") == 6
    assert "fct_segment_aspect" in examples


def test_gold_questions_are_unique_and_categorised() -> None:
    ids = [g.id for g in GOLD]
    assert len(ids) == len(set(ids)), "duplicate gold id"
    assert len(GOLD) >= 25
    assert {g.category for g in GOLD} <= {"simple", "aggregate", "join", "trap", "ranking"}
    for gold in GOLD:
        assert "{m}" in gold.sql, f"{gold.id} hardcodes a schema"


def test_ceilings_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("OLIST_DEMO_LIFETIME_USD", "5.50")
    monkeypatch.setenv("OLIST_DEMO_SESSION_CALLS", "42")
    values = settings.gemini_budget_settings()
    assert values["lifetime_usd"] == 5.50
    assert values["max_calls_per_session"] == 42


def test_an_unset_variable_keeps_the_documented_default(monkeypatch) -> None:
    monkeypatch.delenv("OLIST_DEMO_LIFETIME_USD", raising=False)
    from analytics import gemini_budget

    assert settings.gemini_budget_settings()["lifetime_usd"] == gemini_budget.DEFAULT_LIFETIME_USD


def test_a_typod_ceiling_fails_loudly_instead_of_reverting(monkeypatch) -> None:
    """
    The failure this prevents: OLIST_DEMO_LIFETIME_USD="5.00 " with a stray
    character silently falling back to $2.00, so the operator believes they
    raised the ceiling and the demo stops days early.
    """
    monkeypatch.setenv("OLIST_DEMO_LIFETIME_USD", "five dollars")
    with pytest.raises(BadSetting, match="not a valid float"):
        settings.gemini_budget_settings()


def test_query_ceilings_are_configurable_too(monkeypatch) -> None:
    monkeypatch.setenv("OLIST_BQ_MAX_BYTES_PER_QUERY", str(2 * 1024**3))
    assert settings.query_budget_settings()["max_bytes_per_query"] == 2 * 1024**3
