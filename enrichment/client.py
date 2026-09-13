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

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Retryable: rate limits and transient server faults. A schema or auth failure is
# not retryable and must surface immediately rather than after five backoffs.
RETRYABLE = ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")


@dataclass(frozen=True)
class Usage:
    """What the API says it processed. Never what we guessed it would."""

    input_tokens: int
    output_tokens: int
    wall_seconds: float


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


class GeminiClient:
    """Labels a batch of reviews and reports what it cost."""

    def __init__(self, model: str, max_attempts: int = 6, client=None) -> None:
        self.model = model
        self.max_attempts = max_attempts
        self._client = client or make_client()

    def generate(self, prompt: str, schema: dict) -> tuple[str, Usage]:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
        )

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
            return response.text, Usage(
                input_tokens=meta.prompt_token_count or 0,
                output_tokens=(meta.candidates_token_count or 0),
                wall_seconds=elapsed,
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
