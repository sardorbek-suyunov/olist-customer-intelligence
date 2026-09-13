"""
Everything except the HTTP call, exercised offline.

The network is the only part a key is needed for, and `make gemini-check` covers
that separately. The cache, the hash, the batch parser, the quarantine and the
cost log are all deterministic, so they are tested here and run in CI with no
credentials.

The parser tests matter most. Batching 20 reviews into one call is a 10x cost
lever and a new failure mode at the same time: a response that quietly answers 19
of 20. Every one of those cases is asserted rather than assumed.
"""

from __future__ import annotations

import json

import pytest

from enrichment import taxonomy
from enrichment.client import ScriptedClient, Usage
from enrichment.enrich import build_prompt, main, parse_batch
from enrichment.pricing import cost_usd, price_for
from enrichment.store import DuckDBStore, content_hash

MODEL = "gemini-3.7-flash-test"
VERSION = taxonomy.PROMPT_VERSION


def batch_of(n: int) -> tuple[list[tuple[str, str]], list[str]]:
    batch = [(f"r{i}", f"texto {i}") for i in range(1, n + 1)]
    hashes = [content_hash(t, VERSION, MODEL) for _, t in batch]
    return batch, hashes


def answer(i: int, aspects: list[str], sentiment="negative", severity=2, empty=False) -> dict:
    return {
        "i": i,
        "aspects": aspects,
        "sentiment": sentiment,
        "severity": severity,
        "no_content": empty,
    }


# ---------------------------------------------------------------------------
# the hash IS the cache key
# ---------------------------------------------------------------------------
def test_hash_is_stable_for_identical_text() -> None:
    assert content_hash("igual", VERSION, MODEL) == content_hash("igual", VERSION, MODEL)


def test_prompt_version_invalidates_the_cache() -> None:
    """A prompt edit must not serve enrichments made under the old definition."""
    assert content_hash("t", "v1", MODEL) != content_hash("t", "v2", MODEL)


def test_model_invalidates_the_cache() -> None:
    assert content_hash("t", VERSION, "a") != content_hash("t", VERSION, "b")


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------
def test_full_batch_parses() -> None:
    batch, hashes = batch_of(3)
    raw = json.dumps([answer(1, ["delivery_late"]), answer(2, []), answer(3, ["wrong_item"])])

    labelled, quarantined = parse_batch(raw, batch, hashes, MODEL)

    assert len(labelled) == 3
    assert quarantined == []
    assert labelled[0].aspects == ["delivery_late"]


def test_a_review_missing_from_the_response_is_quarantined_individually() -> None:
    """
    The batching failure. A response covering 19 of 20 must not lose the
    twentieth silently -- that review is recorded with a reason, not dropped.
    """
    batch, hashes = batch_of(20)
    raw = json.dumps([answer(i, []) for i in range(1, 20)])  # 1..19, no 20

    labelled, quarantined = parse_batch(raw, batch, hashes, MODEL)

    assert len(labelled) == 19
    assert len(quarantined) == 1
    assert quarantined[0].content_hash == hashes[19]
    assert quarantined[0].reason == "missing_from_response"


def test_unparseable_response_quarantines_the_whole_batch() -> None:
    batch, hashes = batch_of(5)

    labelled, quarantined = parse_batch("not json at all", batch, hashes, MODEL)

    assert labelled == []
    assert len(quarantined) == 5
    assert all(q.reason.startswith("unparseable_response") for q in quarantined)
    assert quarantined[0].raw_response == "not json at all"


def test_invented_aspect_is_rejected_not_stored() -> None:
    """Schema enforcement is server-side; this is the belt to that brace."""
    batch, hashes = batch_of(2)
    raw = json.dumps([answer(1, ["delivery_late"]), answer(2, ["vibes_were_off"])])

    labelled, quarantined = parse_batch(raw, batch, hashes, MODEL)

    assert len(labelled) == 1
    assert len(quarantined) == 1
    assert "unknown_aspect" in quarantined[0].reason


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"sentiment": "furious"}, "bad_sentiment"),
        ({"severity": 9}, "bad_severity"),
        ({"severity": "high"}, "bad_severity"),
    ],
)
def test_out_of_range_fields_are_quarantined(bad: dict, reason: str) -> None:
    batch, hashes = batch_of(1)
    raw = json.dumps([{**answer(1, []), **bad}])

    labelled, quarantined = parse_batch(raw, batch, hashes, MODEL)

    assert labelled == []
    assert reason in quarantined[0].reason


def test_duplicate_index_keeps_the_first_and_quarantines_the_repeat() -> None:
    batch, hashes = batch_of(2)
    raw = json.dumps([answer(1, ["delivery_late"]), answer(1, ["wrong_item"]), answer(2, [])])

    labelled, quarantined = parse_batch(raw, batch, hashes, MODEL)

    assert len(labelled) == 2
    assert any(q.reason == "duplicate_index" for q in quarantined)


# ---------------------------------------------------------------------------
# the prompt and the taxonomy stay in step
# ---------------------------------------------------------------------------
def test_prompt_lists_every_aspect() -> None:
    """The prompt is generated from the taxonomy; nothing is typed twice."""
    prompt = build_prompt([("r1", "texto")])
    for name in taxonomy.ASPECT_NAMES:
        assert name in prompt


def test_schema_enum_matches_the_taxonomy() -> None:
    enum = taxonomy.response_schema()["items"]["properties"]["aspects"]["items"]["enum"]
    assert enum == list(taxonomy.ASPECT_NAMES)


def test_delivery_complaint_aspects_all_exist() -> None:
    assert set(taxonomy.ASPECT_NAMES) >= taxonomy.DELIVERY_COMPLAINT_ASPECTS


def test_every_merge_parent_is_a_real_aspect() -> None:
    for aspect in taxonomy.ASPECTS:
        if aspect.parent is not None:
            assert aspect.parent in taxonomy.BY_NAME, aspect.name


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
def test_batch_pricing_is_half_of_standard() -> None:
    model = "gemini-3.7-flash"
    standard = cost_usd(model, 1_000_000, 1_000_000, batch=False)
    batched = cost_usd(model, 1_000_000, 1_000_000, batch=True)
    assert batched == pytest.approx(standard / 2)


def test_unpriced_model_logs_zero_rather_than_a_guess() -> None:
    assert price_for("some-future-model") is None
    assert cost_usd("some-future-model", 1_000_000, 1_000_000) == 0.0


# ---------------------------------------------------------------------------
# the store, and the $0 re-run
# ---------------------------------------------------------------------------
def test_cache_hit_means_no_call_and_no_cost(tmp_path, monkeypatch) -> None:
    """
    The claim that a re-run with an unchanged prompt costs exactly $0,
    demonstrated rather than asserted: the second run makes no calls and logs
    zero tokens and zero dollars.
    """
    db = tmp_path / "enrich.duckdb"
    reviews = [("r1", "não recebi o produto"), ("r2", "ótimo")]
    monkeypatch.setattr("enrichment.enrich.load_reviews", lambda *_, **__: reviews)

    responses = [
        json.dumps([answer(1, ["not_received"], "negative", 3), answer(2, [], "positive", 0, True)])
    ]
    first = ScriptedClient(responses, Usage(500, 120, 0.5))
    assert (
        main(
            ["--model", MODEL, "--sample", "2", "--batch-size", "20", "--duckdb-path", str(db)],
            client=first,
        )
        == 0
    )
    assert len(first.calls) == 1

    # Second run: same prompt version, same model, same text.
    second = ScriptedClient([], Usage(0, 0, 0.0))
    assert (
        main(
            ["--model", MODEL, "--sample", "2", "--batch-size", "20", "--duckdb-path", str(db)],
            client=second,
        )
        == 0
    )
    assert second.calls == [], "a cached re-run must make no API calls"

    store = DuckDBStore(db)
    try:
        totals = store.totals()
        runs = store.con.execute(
            "select cost_usd, input_tokens, output_tokens, reviews "
            "from olist_raw.enrichment_cost_log order by created_at"
        ).fetchall()
    finally:
        store.close()

    assert runs[-1] == (0.0, 0, 0, 0), "the re-run must log zero cost and zero tokens"
    assert totals["cost_usd"] == pytest.approx(runs[0][0])


def test_duplicate_text_is_labelled_once(tmp_path, monkeypatch) -> None:
    """13% of the corpus is duplicate text; it must cost one call, not two."""
    db = tmp_path / "dupes.duckdb"
    reviews = [("r1", "Ótimo"), ("r2", "Ótimo"), ("r3", "Ótimo")]
    monkeypatch.setattr("enrichment.enrich.load_reviews", lambda *_, **__: reviews)

    client = ScriptedClient([json.dumps([answer(1, [], "positive", 0, True)])])
    assert (
        main(
            ["--model", MODEL, "--sample", "3", "--batch-size", "20", "--duckdb-path", str(db)],
            client=client,
        )
        == 0
    )

    store = DuckDBStore(db)
    try:
        results = store.con.execute("select count(*) from olist_raw.review_enrichment").fetchone()[
            0
        ]
        mapped = store.con.execute(
            "select count(*) from olist_raw.review_enrichment_map"
        ).fetchone()[0]
    finally:
        store.close()

    assert len(client.calls) == 1
    assert results == 1, "one distinct text, one stored label"
    assert mapped == 3, "all three reviews still point at it"


def test_quarantined_rows_are_written_not_dropped(tmp_path, monkeypatch) -> None:
    db = tmp_path / "quarantine.duckdb"
    reviews = [("r1", "texto um"), ("r2", "texto dois")]
    monkeypatch.setattr("enrichment.enrich.load_reviews", lambda *_, **__: reviews)

    client = ScriptedClient(["{ not json"])
    assert (
        main(
            ["--model", MODEL, "--sample", "2", "--batch-size", "20", "--duckdb-path", str(db)],
            client=client,
        )
        == 0
    )

    store = DuckDBStore(db)
    try:
        rows = store.con.execute("select reason from olist_raw.enrichment_quarantine").fetchall()
    finally:
        store.close()

    assert len(rows) == 2
    assert all("unparseable_response" in r[0] for r in rows)


def test_max_calls_is_a_spend_ceiling(tmp_path, monkeypatch) -> None:
    """Resumable by construction: stop early, re-run, the cache covers the rest."""
    db = tmp_path / "ceiling.duckdb"
    reviews = [(f"r{i}", f"texto {i}") for i in range(1, 7)]
    monkeypatch.setattr("enrichment.enrich.load_reviews", lambda *_, **__: reviews)

    responses = [json.dumps([answer(1, []), answer(2, [])]) for _ in range(3)]
    client = ScriptedClient(responses)
    assert (
        main(
            [
                "--model",
                MODEL,
                "--sample",
                "6",
                "--batch-size",
                "2",
                "--max-calls",
                "1",
                "--duckdb-path",
                str(db),
            ],
            client=client,
        )
        == 0
    )

    assert len(client.calls) == 1
    store = DuckDBStore(db)
    try:
        stored = store.con.execute("select count(*) from olist_raw.review_enrichment").fetchone()[0]
    finally:
        store.close()
    assert stored == 2, "only the completed batch is stored"
