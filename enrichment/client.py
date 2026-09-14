"""
Gemini client: structured output, retries, and honest token accounting.

Two things this deliberately does not do.

It does not hardcode a model id. Model names and prices move, and the figures
this project quoted came off a web page rather than off the API. `list_models()`
asks the key what it can actually reach, and `make-gemini-check` is the gate that
runs before a pilot spends anything.

It does not estimate tokens. Every call records `usage_metadata` from the
response, so the cost log holds what the API billed rather than a ratio applied
to a character count. The chars/3.5 estimate that sized this workload is a
planning number and is replaced, not confirmed, by the first real run.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("olist.gemini")

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Retryable: rate limits and transient server faults. A schema or auth failure is
# not retryable and must surface immediately rather than after five backoffs.
RETRYABLE = ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")


@dataclass(frozen=True)
class Usage:
    """
    What the API says it processed. Never what we guessed it would.

    `output_tokens` is candidates PLUS thoughts, because Gemini 3.x bills both at
    the output rate and reports them separately. Recording candidates alone
    undercounted billed output by 5x on a batch of 20, and the token log said
    $0.66 where the console said $1.39. The log is a self-report; the console is
    the thing -- so the log now reports what the console charges for.
    """

    input_tokens: int
    output_tokens: int
    wall_seconds: float
    candidates_tokens: int = 0
    thoughts_tokens: int = 0


class GeminiError(RuntimeError):
    pass


def _decode(raw: bytes) -> str:
    """
    Decode .env whatever encoding the shell that wrote it chose.

    `echo "KEY=..." > .env` in Windows PowerShell 5.1 writes UTF-16 LE with a
    BOM, so a straight utf-8 read dies on byte 0xff before it ever reaches the
    key. Assuming utf-8 here would make the documented setup step fail on the
    platform this repository is developed on.
    """
    for bom, encoding in (
        (b"\xff\xfe", "utf-16"),  # not utf-16-le: that leaves the BOM as U+FEFF
        (b"\xfe\xff", "utf-16"),  # glued to the first variable's NAME
        (b"\xef\xbb\xbf", "utf-8-sig"),
    ):
        if raw.startswith(bom):
            return raw.decode(encoding).lstrip("﻿")
    return raw.decode("utf-8").lstrip("﻿")


def load_env() -> None:
    """
    Read .env into the environment without echoing it anywhere.

    .env is gitignored. The key is never printed, logged, or written to the
    warehouse -- the cost log records the model name, not the credential.
    """
    if not ENV_FILE.exists():
        return
    for line in _decode(ENV_FILE.read_bytes()).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key() -> str:
    load_env()
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    raise GeminiError(
        "No API key. Put GEMINI_API_KEY=... in a .env file at the repository root "
        "(already gitignored), or set it in the environment. It is never printed "
        "or stored."
    )


def make_client():
    from google import genai

    return genai.Client(api_key=api_key())


def list_models(client) -> list[str]:
    """Every model this key can actually call, newest naming included."""
    out = []
    for model in client.models.list():
        actions = getattr(model, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            out.append(model.name.removeprefix("models/"))
    return sorted(out)


def with_retries(call, max_attempts: int = 6, what: str = "call"):
    """
    Retry a transient failure with full jitter. The ONE implementation.

    Extracted from GeminiClient.generate because the embedding path needed the
    same behaviour and went without it: a full-corpus embed run died on a 429
    after 3,187 vectors, having called the SDK directly. The labelling path
    retried correctly the whole time, which is what made the gap easy to miss --
    the retry existed, it was just welded to the wrong method.

    Full jitter rather than plain exponential backoff: without it a batch that
    hits a rate limit retries in lockstep with every other in-flight batch and
    hits the same limit at the same instant.
    """
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - classified here, not swallowed
            last = exc
            if not any(token in str(exc) for token in RETRYABLE):
                raise GeminiError(f"non-retryable {what}: {exc}") from exc
            delay = random.uniform(0, min(60.0, 2.0**attempt))
            LOG.warning(
                "%s hit a retryable error (attempt %s/%s), backing off %.1fs",
                what,
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(delay)
    raise GeminiError(f"exhausted {max_attempts} attempts on {what}: {last}")


def warn_if_unlogged(model: str, sink: object | None) -> None:
    """
    Say so, loudly, when a billable call has nowhere to be recorded.

    Three exploratory calls made straight through this client once spent $0.0863
    that no cost row recorded, and nothing surfaced it -- the log simply did not
    know they had happened. The log is what the README quotes, so an omission
    understates in the direction nobody checks.

    A callback cannot be made mandatory without breaking every legitimate ad-hoc
    use, so the absence is made audible instead: any client built without a sink
    warns on construction, and the warning names the spend that will go
    unrecorded. A probe is still allowed. It is no longer silent.
    """
    if sink is None:
        LOG.warning(
            "%s client has no cost sink: calls made through it will NOT appear in the "
            "cost log. Fine for a probe -- record it by hand afterwards, or the log "
            "will understate what was spent.",
            model,
        )


class GeminiClient:
    """Labels a batch of reviews and reports what it cost."""

    def __init__(
        self,
        model: str,
        max_attempts: int = 6,
        client=None,
        thinking_budget: int | None = None,
        cost_sink=None,
    ) -> None:
        self.model = model
        self.max_attempts = max_attempts
        self.thinking_budget = thinking_budget
        self.cost_sink = cost_sink
        self._client = client or make_client()
        warn_if_unlogged(model, cost_sink)

    def generate(self, prompt: str, schema: dict) -> tuple[str, Usage]:
        from google.genai import types

        options: dict = {
            "response_mime_type": "application/json",
            "response_schema": schema,
            "temperature": 0.0,
        }
        if self.thinking_budget is not None:
            # Thinking is on by default and dominates the bill: 3,774 thought
            # tokens against 730 of answer on a batch of 20. Whether turning it
            # off costs accuracy is an eval question, not a cost question.
            options["thinking_config"] = types.ThinkingConfig(thinking_budget=self.thinking_budget)
        config = types.GenerateContentConfig(**options)

        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
            except Exception as exc:  # noqa: BLE001 - classified below, not swallowed
                last = exc
                if not any(token in str(exc) for token in RETRYABLE):
                    raise GeminiError(f"non-retryable: {exc}") from exc
                # Full jitter. Without it, a batch that hits a rate limit retries
                # in lockstep and hits it again at the same instant.
                delay = random.uniform(0, min(60.0, 2.0**attempt))
                time.sleep(delay)
                continue

            elapsed = time.monotonic() - started
            meta = response.usage_metadata
            candidates = meta.candidates_token_count or 0
            thoughts = meta.thoughts_token_count or 0
            return response.text, Usage(
                input_tokens=meta.prompt_token_count or 0,
                output_tokens=candidates + thoughts,
                wall_seconds=elapsed,
                candidates_tokens=candidates,
                thoughts_tokens=thoughts,
            )

        raise GeminiError(f"exhausted {self.max_attempts} attempts: {last}")

    def count_tokens(self, text: str) -> int:
        return self._client.models.count_tokens(model=self.model, contents=text).total_tokens


class ScriptedClient:
    """
    Offline stand-in so everything except the network is testable.

    The cache, the batching, the parser, the quarantine and the cost log are all
    exercised against this. Only the HTTP call is unproven without a key, and the
    gate covers that separately.
    """

    def __init__(self, responses: list[str], usage: Usage | None = None) -> None:
        self.responses = list(responses)
        self.usage = usage or Usage(100, 50, 0.01)
        self.calls: list[str] = []

    def generate(self, prompt: str, schema: dict) -> tuple[str, Usage]:
        self.calls.append(prompt)
        if not self.responses:
            raise GeminiError("ScriptedClient ran out of responses")
        return self.responses.pop(0), self.usage
