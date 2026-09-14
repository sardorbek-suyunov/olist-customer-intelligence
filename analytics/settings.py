"""
Deployment knobs, read from the environment, in one place.

The ceilings in `bq_safety` and `gemini_budget` are module constants because a
default has to live somewhere and a constant is honest about being a default.
They are not, however, things you should have to edit code to change: raising
the demo's lifetime budget after topping up the API key is a deployment
decision, not a code change, and a deployment decision that requires a commit
gets made by editing the running container instead.

So every ceiling is overridable by an environment variable with the same name
as the constant, prefixed. Nothing here invents a value: if the variable is
unset the module default applies, and if it is set but unparseable that is an
error rather than a silent fallback to the default -- a typo'd ceiling that
quietly reverts to $2.00 is exactly the kind of thing nobody notices until the
key is empty.

    OLIST_DEMO_LIFETIME_USD=5.00     raise the demo's total allowance
    OLIST_DEMO_DAY_USD=0.10          raise the daily allowance
    OLIST_DEMO_SESSION_USD=0.02      per visitor session
    OLIST_DEMO_SESSION_CALLS=20      per visitor session
    OLIST_DEMO_RESERVE_USD=0.25      never spendable by the demo
    OLIST_BQ_MAX_BYTES_PER_QUERY     bytes, integer
    OLIST_BQ_MAX_BYTES_PER_SESSION   bytes, integer
    OLIST_BQ_MAX_QUERIES_PER_SESSION integer
    OLIST_NL2SQL_MAX_ROWS            rows the agent may return
"""

from __future__ import annotations

import os


class BadSetting(RuntimeError):
    """An environment variable was set to something that is not a number."""


def _read(name: str, default, cast):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError) as exc:
        raise BadSetting(
            f"{name}={raw!r} is not a valid {cast.__name__}. Refusing to fall back to "
            f"the default ({default}): a ceiling that silently reverts is worse than "
            "one that fails loudly."
        ) from exc


def usd(name: str, default: float) -> float:
    return _read(name, default, float)


def count(name: str, default: int) -> int:
    return _read(name, default, int)


def gemini_budget_settings() -> dict[str, object]:
    """Overrides for DemoBudget, keyed by its own field names."""
    from analytics import gemini_budget as g

    return {
        "max_usd_per_session": usd("OLIST_DEMO_SESSION_USD", g.DEFAULT_MAX_USD_PER_SESSION),
        "max_calls_per_session": count("OLIST_DEMO_SESSION_CALLS", g.DEFAULT_MAX_CALLS_PER_SESSION),
        "max_usd_per_day": usd("OLIST_DEMO_DAY_USD", g.DEFAULT_MAX_USD_PER_DAY),
        "lifetime_usd": usd("OLIST_DEMO_LIFETIME_USD", g.DEFAULT_LIFETIME_USD),
        "reserve_usd": usd("OLIST_DEMO_RESERVE_USD", g.DEFAULT_RESERVE_USD),
    }


def query_budget_settings() -> dict[str, int]:
    """Overrides for bq_safety.QueryBudget, keyed by its own field names."""
    from analytics import bq_safety as b

    return {
        "max_bytes_per_query": count("OLIST_BQ_MAX_BYTES_PER_QUERY", b.DEFAULT_MAX_BYTES_PER_QUERY),
        "max_bytes_per_session": count(
            "OLIST_BQ_MAX_BYTES_PER_SESSION", b.DEFAULT_MAX_BYTES_PER_SESSION
        ),
        "max_queries_per_session": count(
            "OLIST_BQ_MAX_QUERIES_PER_SESSION", b.DEFAULT_MAX_QUERIES_PER_SESSION
        ),
    }


def nl2sql_max_rows(default: int = 200) -> int:
    return count("OLIST_NL2SQL_MAX_ROWS", default)
