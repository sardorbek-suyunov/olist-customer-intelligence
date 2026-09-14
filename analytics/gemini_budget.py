"""
Platform-enforced spend ceilings for the PUBLIC demo's Gemini calls.

`bq_safety` stops the agent scanning the warehouse into a bill. This stops the
other half of the same problem: the agent is behind a public URL, every visitor
question is an API call against one key with a finite balance, and a portfolio
link that is dead because a few hundred people clicked it is the worst outcome
this repository can produce. A dead demo is worse than no demo, because the link
is already on the CV.

The design mirrors bq_safety deliberately, because the shape of the problem is
the same and the answer should look the same.

  1. PRICE THE CALL BEFORE MAKING IT. bq_safety dry-runs the query to learn its
     bytes. There is no dry run for generateContent, but `countTokens` is free
     and exact, and `max_output_tokens` bounds the other side. So the MAXIMUM
     cost of a call is known before it is made, and a call that cannot be
     afforded is never issued. This is the dry run, in the only form available.
  2. PER-SESSION CEILING. One visitor cannot spend the month in one sitting.
  3. PER-DAY AND LIFETIME CEILINGS, SHARED ACROSS SESSIONS. The per-session cap
     cannot see a thousand visitors taking one turn each, which is exactly what
     a link going around looks like. The ledger is on disk and shared.

And the control that matters most, which is not a ceiling at all:

  4. THE DEMO IS USEFUL AT ZERO SPEND. A set of example questions ship with
     their SQL and their answers precomputed at build time, served with no API
     call and no warehouse scan. Live NL->SQL is the bonus path. When the budget
     is gone the demo degrades to the cached set with an explanation, rather
     than erroring -- a visitor who never clicks "ask your own" cannot tell the
     difference, and one who does gets a reason instead of a stack trace.

Costs are computed by `enrichment.pricing`, which is the only place in this
repository that knows a token price. Recording usage uses the API's own
`usage_metadata` -- thoughts included -- rather than the estimate, because the
estimate is what we were allowed to spend and the metadata is what we spent.

No network and no GCP calls at import time, so all of this is unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from enrichment.pricing import cost_usd, price_for

LOG = logging.getLogger("olist.gemini_budget")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = Path(
    os.environ.get("OLIST_DEMO_LEDGER", ROOT / "dashboard" / "data" / "demo_ledger.json")
)

# Sized against the real balance rather than against a round number.
#
# One NL->SQL turn is roughly a 1,500-token schema prompt and 200 tokens of SQL,
# which on gemini-3.1-flash-lite is about $0.0007. So $0.05/day is ~70 live
# questions a day and the lifetime cap is a few thousand. That is far more than
# a portfolio link realistically draws, and the cached examples carry the load
# regardless.
#
# RESERVE_USD is the part that is never spendable by the demo at all. The key is
# shared with the enrichment work, and a demo that drains it would take the
# ability to re-run a label pass with it.
DEFAULT_MAX_USD_PER_SESSION = 0.01
DEFAULT_MAX_CALLS_PER_SESSION = 10
DEFAULT_MAX_USD_PER_DAY = 0.05
DEFAULT_LIFETIME_USD = 2.00
DEFAULT_RESERVE_USD = 0.25

# Bounds the unknown half of the cost estimate. SQL for this schema is short;
# anything longer is a runaway, and truncating it is the correct outcome.
DEFAULT_MAX_OUTPUT_TOKENS = 512

# One lock per ledger file, shared by every session in this process.
#
# The first version gave each DemoBudget its own lock, which is worse than no
# lock: it looks like mutual exclusion and provides none, because the thing
# being protected is shared between instances and the locks are not. Ten
# simultaneous visitors each held their own lock and all read the same stale
# ledger. The concurrency test caught it.
#
# This makes the ledger safe across THREADS in one process, which is what a
# Streamlit app is. It does NOT make it safe across processes -- two instances
# on separate hosts would each keep their own count. That needs a real shared
# store, and until the demo runs that way the honest thing is to say so here
# rather than to imply a guarantee the code does not provide.
_LEDGER_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = Path(path).resolve()
    with _LOCKS_GUARD:
        return _LEDGER_LOCKS.setdefault(key, threading.Lock())


class DemoBudgetExceeded(RuntimeError):
    """
    Raised when a call cannot be afforded. Carries which ceiling stopped it so
    the UI can say something true rather than 'something went wrong'.
    """

    def __init__(self, scope: str, message: str) -> None:
        super().__init__(message)
        self.scope = scope


class UnpricedModel(RuntimeError):
    """
    Raised when the model has no price on file.

    `cost_usd` returns 0.0 for an unpriced model, which is right for a log -- a
    visible gap beats an invented number -- and catastrophic for a budget, where
    it would read as 'this call is free' and let the demo spend without limit.
    The same value means different things in the two places, so the budget
    refuses instead of inheriting the log's convention.
    """


@dataclass
class Ledger:
    """Durable, shared spend record. Small enough that JSON is the right store."""

    day: str = ""
    day_usd: float = 0.0
    lifetime_usd: float = 0.0
    calls: int = 0

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls(day=date.today().isoformat())
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: raw[k] for k in raw if k in cls.__annotations__})
        except (json.JSONDecodeError, TypeError, ValueError):
            # A corrupt ledger must not read as "nothing spent yet". Failing
            # closed costs a day of demo; failing open costs the balance.
            LOG.error("ledger at %s is unreadable; treating the day as exhausted", path)
            return cls(day=date.today().isoformat(), day_usd=float("inf"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name per writer. A shared ".tmp" is itself a race: on
        # Windows the replace fails outright while another thread holds it, and
        # on POSIX it would silently publish whichever writer finished last.
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic, so a crash mid-write cannot blank the ledger

    def roll_day(self) -> None:
        today = date.today().isoformat()
        if self.day != today:
            self.day, self.day_usd = today, 0.0


@dataclass
class DemoBudget:
    """
    One visitor session, checked against both its own cap and the shared ones.

    Session state is in memory; day and lifetime state is on disk and shared, so
    concurrent sessions cannot each spend the daily allowance.
    """

    model: str
    ledger_path: Path = DEFAULT_LEDGER
    max_usd_per_session: float = DEFAULT_MAX_USD_PER_SESSION
    max_calls_per_session: int = DEFAULT_MAX_CALLS_PER_SESSION
    max_usd_per_day: float = DEFAULT_MAX_USD_PER_DAY
    lifetime_usd: float = DEFAULT_LIFETIME_USD
    reserve_usd: float = DEFAULT_RESERVE_USD
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    session_usd: float = 0.0
    session_calls: int = 0
    _pending_usd: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if price_for(self.model) is None:
            raise UnpricedModel(
                f"{self.model} has no price in enrichment/pricing.py. cost_usd() "
                "returns 0.0 for an unpriced model, which a budget would read as "
                "free. Add the published rate before putting it behind a public URL."
            )

    def worst_case_usd(self, input_tokens: int) -> float:
        """
        The most this call can cost, not the most it probably costs.

        Input is exact -- countTokens, free, before the call. Output is bounded
        by max_output_tokens, which the request also sets, so the model cannot
        exceed it. A budget built on an average would be wrong precisely on the
        call that mattered.
        """
        return cost_usd(self.model, input_tokens, self.max_output_tokens)

    def check(self, input_tokens: int) -> float:
        """
        Refuse the call if its worst case cannot be afforded, and RESERVE it.

        Checking without reserving would not be a ceiling. The API call happens
        between check and record, and two visitors arriving in that window would
        both be told yes against the same remaining balance -- the classic
        check-then-act race, on the one number that must not go negative. So the
        worst case is written to the ledger up front and settled afterwards.

        A call that is never settled leaves its reservation standing. That
        over-counts, which is the direction to be wrong in.

        Ceilings are checked in order of how surprising the refusal is, so the
        message names the one a visitor actually hit.
        """
        cost = self.worst_case_usd(input_tokens)

        if self.session_calls >= self.max_calls_per_session:
            raise DemoBudgetExceeded(
                "session",
                f"This session has used its {self.max_calls_per_session} live questions. "
                "The example questions below still work -- they are precomputed.",
            )
        if self.session_usd + cost > self.max_usd_per_session:
            raise DemoBudgetExceeded(
                "session",
                "This session has used its share of the demo budget. The example "
                "questions below still work -- they are precomputed.",
            )

        with _lock_for(self.ledger_path):
            ledger = Ledger.load(self.ledger_path)
            ledger.roll_day()
            if ledger.day_usd + cost > self.max_usd_per_day:
                raise DemoBudgetExceeded(
                    "day",
                    "The demo has used today's budget for live questions. It resets "
                    "tomorrow; the example questions below are precomputed and always work.",
                )
            if ledger.lifetime_usd + cost > self.lifetime_usd - self.reserve_usd:
                raise DemoBudgetExceeded(
                    "lifetime",
                    "Live questions are switched off: this demo runs on a real API key "
                    "with a fixed balance, and it has reached the cap set for it. The "
                    "example questions below are precomputed and still work.",
                )
            ledger.day_usd += cost
            ledger.lifetime_usd += cost
            ledger.save(self.ledger_path)

        self._pending_usd += cost
        self.session_usd += cost
        self.session_calls += 1
        return cost

    def record(self, input_tokens: int, output_tokens: int) -> float:
        """
        Settle the reservation against what the call actually cost.

        The estimate was permission to spend; `usage_metadata` is the spend, and
        the gap between the two is exactly where this project's cost log went
        wrong four times. `output_tokens` must already include thoughts --
        `enrichment.client.Usage` sums them, and this is the interface that
        expects it.

        Almost always a refund: the reservation priced output at the cap and the
        model answered in a fraction of it.
        """
        spent = cost_usd(self.model, input_tokens, output_tokens)
        reserved, self._pending_usd = self._pending_usd, 0.0
        delta = spent - reserved

        self.session_usd += delta
        with _lock_for(self.ledger_path):
            ledger = Ledger.load(self.ledger_path)
            ledger.roll_day()
            ledger.day_usd = max(0.0, ledger.day_usd + delta)
            ledger.lifetime_usd = max(0.0, ledger.lifetime_usd + delta)
            ledger.calls += 1
            ledger.save(self.ledger_path)
        return spent

    def generation_config(self) -> dict[str, object]:
        """
        The output cap, as request config, so a caller cannot forget it.

        The reservation prices output at `max_output_tokens`. That is only a
        worst case if the request actually carries the limit -- otherwise the
        ceiling is a number in a budget object with nothing enforcing it, which
        is the "prompt instructions are not a control" mistake in a new costume.
        Merge this into the GenerateContentConfig rather than restating the cap.
        """
        return {"max_output_tokens": self.max_output_tokens}

    def state(self) -> dict[str, object]:
        """What the UI needs to render a degraded mode honestly."""
        ledger = Ledger.load(self.ledger_path)
        ledger.roll_day()
        spendable = max(0.0, self.lifetime_usd - self.reserve_usd - ledger.lifetime_usd)
        return {
            "live_questions_available": (
                self.session_calls < self.max_calls_per_session
                and ledger.day_usd < self.max_usd_per_day
                and spendable > 0
            ),
            "session_calls_left": max(0, self.max_calls_per_session - self.session_calls),
            "day_usd_left": max(0.0, self.max_usd_per_day - ledger.day_usd),
            "lifetime_usd_left": spendable,
            "lifetime_usd_spent": ledger.lifetime_usd,
        }
