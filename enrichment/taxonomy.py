"""
The aspect taxonomy, defined once.

This module is the only place the aspect list exists. The prompt text, the JSON
schema the API enforces, the dbt seed that validates the mart, and the eval
harness all derive from it. Writing the list a second time -- in a prompt string,
in a schema literal, in a CREATE TABLE -- is the failure this project keeps
finding, so there is one definition and four consumers.

Aspects are polarity-encoded where direction is the whole point
(`delivery_late` vs `delivery_fast`) and polarity-neutral otherwise, with overall
sentiment carrying the rest. That halves output tokens against a per-aspect
sentiment design, and output tokens dominate the cost.

Grounded in reading the corpus, not in what a review taxonomy "should" contain.
`split_shipment` in particular is Olist-specific: one order spans several sellers,
so it arrives in pieces and bills separately, and customers experience that as a
single complaint.
"""

from __future__ import annotations

from dataclasses import dataclass

# The version travels in the cache key. Editing the taxonomy, the prompt text or
# the schema MUST bump it, or a re-run silently serves enrichments produced by a
# definition that no longer exists.
PROMPT_VERSION = "v2"


@dataclass(frozen=True)
class Aspect:
    name: str
    group: str
    # Where this aspect folds if the pilot shows it is too rare to stand alone.
    # None means it is already a parent and cannot be merged upward.
    parent: str | None
    description: str


ASPECTS: tuple[Aspect, ...] = (
    # -- fulfilment ---------------------------------------------------------
    Aspect(
        "delivery_late",
        "fulfilment",
        None,
        "arrived later than promised, or is still late; complaints about the deadline",
    ),
    Aspect(
        "delivery_fast",
        "fulfilment",
        None,
        "arrived early or on time, mentioned approvingly",
    ),
    Aspect(
        "not_received",
        "fulfilment",
        None,
        "never arrived at all, including 'marked delivered but I have nothing'",
    ),
    Aspect(
        "partial_delivery",
        "fulfilment",
        None,
        "some items of a multi-item order missing; paid for N, received fewer",
    ),
    Aspect(
        "split_shipment",
        "fulfilment",
        "delivery_late",
        "one order arrived as several parcels or billed as several charges",
    ),
    Aspect(
        "delivery_carrier_issue",
        "fulfilment",
        "delivery_late",
        "a failed or mishandled delivery attempt; carrier did not ring, wrong address",
    ),
    Aspect(
        "tracking_missing",
        "fulfilment",
        "delivery_late",
        "no tracking code, or tracking that does not reflect reality",
    ),
    Aspect(
        "freight_cost",
        "fulfilment",
        None,
        "the shipping charge itself, usually that it was high for what arrived",
    ),
    # -- product ------------------------------------------------------------
    Aspect(
        "product_defect",
        "product",
        None,
        "arrived broken, damaged or non-functional",
    ),
    Aspect(
        "product_quality",
        "product",
        None,
        # v2. The v1 wording -- "build quality or materials, good or bad, short of
        # an outright defect" -- read as a request for a specific, technical
        # judgement, and the model did not map a plain verdict onto it. It dropped
        # "Bom produto" and "os copos sao lindos" while correctly labelling the
        # delivery aspect of the same sentence. That single omission was 39 of the
        # 85 false negatives in the v1 eval. The change widens the description to
        # include the plain verdict; it does not widen the aspect's meaning.
        "any verdict on the product itself -- build quality, materials, or simply "
        "that it is good or bad -- short of an outright defect. Applies even when "
        "the review also praises delivery or the seller",
    ),
    Aspect("wrong_item", "product", None, "a different product than the one ordered"),
    Aspect(
        "not_as_described",
        "product",
        "product_quality",
        "real but mismatched against the listing: size, colour, count, completeness",
    ),
    Aspect(
        "packaging",
        "product",
        None,
        "how it was packed; inadequate protection, or praise for careful packing",
    ),
    # -- seller -------------------------------------------------------------
    Aspect(
        "seller_unresponsive",
        "seller",
        None,
        "contacted the seller or the site and got no useful answer",
    ),
    Aspect(
        "seller_recommended",
        "seller",
        None,
        "the seller or shop praised, which can happen even when the product is not",
    ),
    Aspect(
        "refund_or_billing",
        "seller",
        None,
        "charges, refunds, cancellations, being billed for something not received",
    ),
)

ASPECT_NAMES: tuple[str, ...] = tuple(a.name for a in ASPECTS)
BY_NAME: dict[str, Aspect] = {a.name: a for a in ASPECTS}

SENTIMENTS = ("positive", "negative", "mixed", "neutral")

# Aspects that make a review a delivery complaint. `is_delivery_complaint` is
# NOT requested from the model: it is derivable from the aspect list, and asking
# for it would create a second source that can contradict the first. dbt derives
# it from this set, which is why the set lives here rather than in SQL.
DELIVERY_COMPLAINT_ASPECTS = frozenset(
    {
        "delivery_late",
        "not_received",
        "partial_delivery",
        "split_shipment",
        "delivery_carrier_issue",
        "tracking_missing",
    }
)

# ---------------------------------------------------------------------------
# Support thresholds. Fixed BEFORE the pilot ran, so the numbers cannot be
# chosen to flatter whatever came back.
# ---------------------------------------------------------------------------

# An aspect must appear at least this often in the 2,000-review pilot to stand on
# its own. Below it the aspect is either genuinely marginal or the model is
# confusing it with a neighbour, and either way it folds into `parent` -- or, if
# it has no parent, stays extractable but is excluded from the scored eval with
# the exclusion reported.
PILOT_MIN_OCCURRENCES = 20  # 1% of the pilot

# A per-aspect F1 is only published with at least this many positives in the eval
# sample. The binomial 95% interval on recall has half-width of roughly 1/sqrt(n):
# at n=30 that is already +/-18pp, and below it the number carries no information
# worth printing. Aspects under the floor are reported as "insufficient support"
# with their counts, never as a noisy score.
EVAL_MIN_POSITIVES = 30


def response_schema() -> dict:
    """
    The JSON schema the API enforces, generated from the taxonomy above.

    Native schema enforcement rather than asking politely in the prompt: a model
    that returns prose instead of JSON is a parsing problem, and a model that
    returns a plausible-but-invented aspect name is a silent data problem.
    """
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "i": {
                    "type": "INTEGER",
                    "description": "the review's index in the batch, starting at 1",
                },
                "aspects": {
                    "type": "ARRAY",
                    "items": {"type": "STRING", "enum": list(ASPECT_NAMES)},
                },
                "sentiment": {"type": "STRING", "enum": list(SENTIMENTS)},
                "severity": {
                    "type": "INTEGER",
                    "description": "0 no complaint, 1 minor, 2 material, 3 order failed",
                },
                "no_content": {
                    "type": "BOOLEAN",
                    "description": "true when the text carries no extractable opinion",
                },
            },
            "required": ["i", "aspects", "sentiment", "severity", "no_content"],
            "propertyOrdering": ["i", "aspects", "sentiment", "severity", "no_content"],
        },
    }


def prompt_preamble() -> str:
    """The instruction text, with the aspect list rendered from the taxonomy."""
    lines = [
        "You label Brazilian e-commerce customer reviews, written in Portuguese.",
        "",
        "For each numbered review, return the aspects it raises, the overall",
        "sentiment, a severity, and whether it carries any extractable opinion.",
        "",
        "Use ONLY these aspects. Assign none if the text raises none:",
        "",
    ]
    group = None
    for aspect in ASPECTS:
        if aspect.group != group:
            group = aspect.group
            lines.append(f"  [{group}]")
        lines.append(f"    {aspect.name}: {aspect.description}")
    lines += [
        "",
        "severity: 0 no complaint, 1 minor, 2 material, 3 the order failed.",
        "no_content: true for bare praise or filler with nothing to extract",
        '  (for example "Ótimo", "Bom", "recomendo") -- still give a sentiment.',
        "",
        "Return one object per review, with `i` matching the review's number.",
        "Return an object for EVERY review, including ones you label empty.",
    ]
    return "\n".join(lines)
