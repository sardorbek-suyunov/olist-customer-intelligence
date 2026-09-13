"""
Token prices, in one place, with a date on them.

These came off Google's pricing page on 2026-09-13 and they move -- the 3.8/3.7
Flash rates below double on 2027-01-01, which is stated in the table rather than
left as a surprise. Nothing else in the codebase knows a price: the cost log
stores dollars computed here, and the README reads the log.

USD per 1,000,000 tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_AS_OF = "2026-09-13"


@dataclass(frozen=True)
class Price:
    standard_in: float
    standard_out: float
    batch_in: float
    batch_out: float
    note: str = ""


# Keyed on the EXACT model id, not a prefix.
#
# Prefix matching was the first attempt and it silently mispriced every variant:
# `gemini-3.5-flash-lite` starts with `gemini-3.5-flash`, so it inherited the
# full-size rate and the cost log would have carried a wrong number that looked
# entirely plausible. An unpriced model is visible -- it logs 0.00 and warns --
# where a wrongly-priced one is not.
MODELS: dict[str, Price] = {
    "gemini-3.8-flash": Price(0.75, 3.75, 0.375, 1.875, "doubles 2027-01-01"),
    "gemini-3.7-flash": Price(0.75, 3.75, 0.375, 1.875, "doubles 2027-01-01"),
    "gemini-3.6-flash": Price(0.75, 3.75, 0.375, 1.875),
    "gemini-3.5-flash": Price(1.50, 9.00, 0.75, 4.50),
    "gemini-3.1-flash-lite": Price(0.25, 1.50, 0.125, 0.75),
    "gemini-3.5-flash-lite": Price(0.30, 2.50, 0.15, 1.25),
    "gemini-2.5-flash-lite": Price(0.10, 0.40, 0.05, 0.20),
}

# Thinking tokens bill at the OUTPUT rate and are reported separately by the API,
# in thoughts_token_count. They are not a surcharge, they are output -- which is
# why enrichment.client sums candidates and thoughts before anything reaches
# cost_usd(). Recording candidates alone is what made the token log disagree with
# the billing console.

# gemini-2.5-flash was listed here at 0.30/2.50 on an assumption that turned out
# to be the 3.5-flash-lite rate. Removed rather than corrected: an unpriced model
# logs 0.00 and warns, and a guess that happens to be a real price for a
# different model is precisely the kind of plausible-looking wrong number this
# module exists to avoid.


def price_for(model: str) -> Price | None:
    return MODELS.get(model)


def cost_usd(model: str, input_tokens: int, output_tokens: int, batch: bool = False) -> float:
    """
    Dollars for a call, or 0.0 for a model with no price on file.

    Returning zero rather than guessing: an unpriced model should show up in the
    log as an obvious gap, not as a plausible number nobody can trace.
    """
    price = price_for(model)
    if price is None:
        return 0.0
    rate_in = price.batch_in if batch else price.standard_in
    rate_out = price.batch_out if batch else price.standard_out
    return input_tokens / 1e6 * rate_in + output_tokens / 1e6 * rate_out
