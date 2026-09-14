"""
The demo budget guard. Every test here is a way the public demo could die.

The failure being defended against is specific: a portfolio link that is dead
because it went around once. So the tests are about ceilings holding under
concurrency and corruption, not about happy paths.
"""

from __future__ import annotations

import json
import threading
from datetime import date

import pytest

from analytics.gemini_budget import (
    DemoBudget,
    DemoBudgetExceeded,
    Ledger,
    UnpricedModel,
)

MODEL = "gemini-3.1-flash-lite"


def budget(tmp_path, **kwargs) -> DemoBudget:
    return DemoBudget(model=MODEL, ledger_path=tmp_path / "ledger.json", **kwargs)


def test_unpriced_model_is_refused_outright() -> None:
    """
    cost_usd returns 0.0 for an unpriced model. In a log that is a visible gap;
    in a budget it reads as "free" and removes every ceiling at once.
    """
    with pytest.raises(UnpricedModel):
        DemoBudget(model="gemini-embedding-001")


def test_worst_case_prices_output_at_the_cap_not_at_an_average(tmp_path) -> None:
    b = budget(tmp_path, max_output_tokens=512)
    # 0.25/M in, 1.50/M out
    assert b.worst_case_usd(1500) == pytest.approx(1500 / 1e6 * 0.25 + 512 / 1e6 * 1.50)


def test_session_call_cap_stops_one_visitor_monopolising(tmp_path) -> None:
    b = budget(tmp_path, max_calls_per_session=3, max_usd_per_session=99.0)
    for _ in range(3):
        b.check(100)
        b.record(100, 50)
    with pytest.raises(DemoBudgetExceeded) as exc:
        b.check(100)
    assert exc.value.scope == "session"


def test_day_cap_is_shared_across_sessions(tmp_path) -> None:
    """
    The case a per-session cap cannot see: many visitors taking one turn each,
    which is what a link going around actually looks like.
    """
    first = budget(tmp_path, max_usd_per_day=0.002)
    first.check(1000)
    first.record(1000, 1000)  # ~0.00175

    second = budget(tmp_path, max_usd_per_day=0.002)  # a different visitor
    with pytest.raises(DemoBudgetExceeded) as exc:
        second.check(1000)
    assert exc.value.scope == "day"


def test_reserve_is_never_spendable(tmp_path) -> None:
    """
    The key is shared with the enrichment work. A demo that drains it takes away
    the ability to re-run a label pass, so part of the balance is not the
    demo's to spend at all.
    """
    b = budget(tmp_path, lifetime_usd=1.0, reserve_usd=0.9, max_usd_per_day=99.0)
    ledger = Ledger(day=date.today().isoformat(), lifetime_usd=0.0999)
    ledger.save(b.ledger_path)
    # 0.0999 of the 0.10 spendable allowance is gone; the next call cannot fit.
    with pytest.raises(DemoBudgetExceeded) as exc:
        b.check(10_000)
    assert exc.value.scope == "lifetime"
    assert b.state()["lifetime_usd_left"] == pytest.approx(0.0001)


def test_a_corrupt_ledger_fails_closed(tmp_path) -> None:
    """
    An unreadable ledger must not read as "nothing spent yet". Failing closed
    costs a day of demo; failing open costs the balance.
    """
    b = budget(tmp_path)
    b.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    b.ledger_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DemoBudgetExceeded):
        b.check(100)


def test_record_uses_actual_usage_not_the_estimate(tmp_path) -> None:
    """
    check() grants permission for the worst case; record() writes what was
    spent. Recording the estimate is how the enrichment cost log drifted from
    the billing console.
    """
    b = budget(tmp_path, max_output_tokens=512)
    granted = b.check(1000)
    spent = b.record(1000, 10)  # the model answered in 10 tokens, not 512
    assert spent < granted
    assert json.loads(b.ledger_path.read_text())["lifetime_usd"] == pytest.approx(spent)


def test_concurrent_sessions_cannot_each_spend_the_daily_cap(tmp_path) -> None:
    """Ten visitors arriving at once still share one daily allowance."""
    path = tmp_path / "ledger.json"
    errors: list[str] = []

    def visitor() -> None:
        b = DemoBudget(model=MODEL, ledger_path=path, max_usd_per_day=0.004)
        try:
            b.check(1000)
            # Output stays under max_output_tokens, which the request also sets.
            # That is what makes the reservation a genuine worst case.
            b.record(1000, 400)
        except DemoBudgetExceeded as exc:
            errors.append(exc.scope)

    threads = [threading.Thread(target=visitor) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spent = json.loads(path.read_text())["lifetime_usd"]
    assert spent <= 0.004 + 1e-9, f"daily cap overrun: {spent}"
    assert errors, "no visitor was refused, so the cap never engaged"


def test_state_reports_degraded_mode_rather_than_raising(tmp_path) -> None:
    """The UI needs to say something true before it tries and fails."""
    b = budget(tmp_path, max_calls_per_session=1)
    assert b.state()["live_questions_available"] is True
    b.check(100)
    b.record(100, 50)
    state = b.state()
    assert state["live_questions_available"] is False
    assert state["session_calls_left"] == 0
