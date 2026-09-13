"""
The gate before any pilot spends money.

    make gemini-check                 # what the key can reach
    make gemini-check MODEL=<id>      # resolve one model and cost a single call

Model ids and prices move, and the figures that sized this workload came off a
web page rather than off the API. This resolves them against the actual key: it
lists what the key can call, makes one real request, prints the tokens the API
reports, and prices them from enrichment/pricing.py.

Never prints the key. The output is model names and token counts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment import taxonomy  # noqa: E402
from enrichment.client import GeminiError, list_models, make_client  # noqa: E402
from enrichment.pricing import PRICES_AS_OF, cost_usd, price_for  # noqa: E402

PROBE = [
    "Produto excelente, entrega rápida!",
    "Não recebi o produto e ninguém respondeu.",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="exact model id to resolve")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="0 disables thinking. Gate the configuration you will actually run: "
        "thinking tokens bill at the output rate and dominated the first pilot.",
    )
    args = parser.parse_args(argv)

    try:
        client = make_client()
    except GeminiError as exc:
        print(f"  FAIL  {exc}")
        return 1

    print("Models this key can call for generateContent:\n")
    try:
        available = list_models(client)
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        print(f"  FAIL  could not list models: {exc}")
        return 1

    flash = [m for m in available if "flash" in m]
    for name in flash or available[:40]:
        priced = price_for(name)
        tag = (
            ""
            if priced is None
            else f"  (priced: {priced.standard_in}/{priced.standard_out} per 1M)"
        )
        print(f"  {name}{tag}")
    print(f"\n  {len(available)} models total, {len(flash)} flash")
    print(f"  prices on file as of {PRICES_AS_OF}")

    if args.list_only or not args.model:
        print("\nPass --model <id> to resolve one and cost a single call.")
        return 0

    if args.model not in available:
        print(f"\n  FAIL  {args.model!r} is not callable by this key.")
        print("        Pick one from the list above; the id must match exactly.")
        return 1
    print(f"\n  ok    {args.model} resolves")

    if price_for(args.model) is None:
        print(f"  WARN  no price on file for {args.model}; cost would log as 0.00")

    print("\nOne real call, two reviews, the production schema:")
    prompt = f"{taxonomy.prompt_preamble()}\n\nReviews:\n" + "\n".join(
        f"{i}. {t}" for i, t in enumerate(PROBE, start=1)
    )
    try:
        from enrichment.client import GeminiClient

        raw, usage = GeminiClient(
            args.model, client=client, thinking_budget=args.thinking_budget
        ).generate(prompt, taxonomy.response_schema())
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  call failed: {exc}")
        return 1

    standard = cost_usd(args.model, usage.input_tokens, usage.output_tokens, batch=False)
    batched = cost_usd(args.model, usage.input_tokens, usage.output_tokens, batch=True)

    print(f"  input tokens  {usage.input_tokens:,}   (API-reported, not estimated)")
    print(
        f"  output tokens {usage.output_tokens:,}"
        f"   = {usage.candidates_tokens:,} answer + {usage.thoughts_tokens:,} thinking"
    )
    print(f"  wall          {usage.wall_seconds:.2f}s")
    print(f"  cost          ${standard:.6f} standard / ${batched:.6f} batch")
    print(f"\n  response: {raw.strip()[:400]}")

    per_review_in = usage.input_tokens / len(PROBE)
    print(
        f"\n  Extrapolation check: {per_review_in:,.0f} input tokens/review at batch size "
        f"{len(PROBE)} — the schema overhead is why batching matters."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
