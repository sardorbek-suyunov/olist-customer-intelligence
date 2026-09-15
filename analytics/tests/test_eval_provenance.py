"""
The README's agent scores must describe the agent that ships.

`make readme-check` already guarantees README.md matches README.template.md
rendered against docs/figures.json. It cannot guarantee the figures describe the
current code, and for one release they did not: scripts/profile_dataset.py read
the scores from a path typed by hand, `nl2sql_v1.json`. The prompt moved to v2
and the path did not, so the README reported 22/25 while shipping the agent that
scores 23/25 -- and the drift gate passed on every run, because the render was
faithful to figures that were faithful to a superseded file.

A green check that cannot fail for the error you are looking for is the same
shape as the ceiling script reporting `0.0 B, infinity x inside the ceiling` over
an empty table. These tests are the question that gate could not ask.

Three links, each of which broke silently at least once:

  shipped prompt  ->  the eval artifact named for it   (version resolution)
  eval artifact   ->  the prompt text that produced it (fingerprint stamp)
  eval artifact   ->  the figures the README renders   (no third copy)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analytics.nl2sql import AGENT_PROMPT_VERSION, prompt_fingerprint

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "enrichment" / "eval"
FIGURES = ROOT / "docs" / "figures.json"

ARTIFACT = EVAL_DIR / f"nl2sql_{AGENT_PROMPT_VERSION}.json"


@pytest.fixture(scope="module")
def artifact() -> dict:
    if not ARTIFACT.exists():
        present = sorted(p.name for p in EVAL_DIR.glob("nl2sql_*.json"))
        pytest.fail(
            f"the agent ships prompt {AGENT_PROMPT_VERSION} and there is no eval "
            f"scored against it. Present: {present or 'none'}. Run:\n"
            "  python scripts/eval_nl2sql.py --backend bigquery"
        )
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def figures() -> dict:
    return json.loads(FIGURES.read_text(encoding="utf-8"))


def test_artifact_was_produced_by_the_shipped_prompt(artifact):
    """
    The stamp, not the filename.

    Deriving the path from AGENT_PROMPT_VERSION catches a bumped version with no
    re-run. It cannot catch the opposite -- editing the rules in place and
    leaving the version alone -- because the filename does not change. The
    fingerprint is over the authored prompt text, so that edit fails here.
    """
    assert artifact["prompt_version"] == AGENT_PROMPT_VERSION
    assert artifact["prompt_fingerprint"] == prompt_fingerprint(), (
        "the rules in analytics/nl2sql.py changed without AGENT_PROMPT_VERSION being "
        "bumped, so this score no longer describes the shipped agent. Re-run the eval, "
        "or restore the prompt."
    )


def test_figures_come_from_that_artifact(artifact, figures):
    """
    The assertion that would have caught the original drift.

    Every NL->SQL number the README prints is checked against the artifact rather
    than against a remembered value, so this test does not need updating when the
    agent improves -- only when the two stop agreeing, which is the only thing it
    is here to notice.
    """
    assert figures["nl2sql_matched"] == artifact["matched"]
    assert figures["nl2sql_total"] == artifact["total"]
    assert figures["nl2sql_accuracy_pct"] == round(100 * artifact["accuracy"], 1)
    assert figures["nl2sql_model"] == artifact["model"]
    assert figures["nl2sql_prompt_version"] == artifact["prompt_version"]

    for category, (matched, total) in artifact["by_category"].items():
        assert figures[f"nl2sql_{category}_matched"] == matched
        assert figures[f"nl2sql_{category}_total"] == total


def test_the_reported_failures_are_the_actual_failures(artifact, figures):
    """
    The README named three failing questions and then described two, four
    paragraphs apart: the list was written against v1, the narrative against v2.
    Both are rendered from here now, so they cannot disagree again.
    """
    mismatches = [r["id"] for r in artifact["results"] if r["outcome"] != "match"]
    assert figures["nl2sql_mismatch_count"] == len(mismatches)
    assert figures["nl2sql_mismatch_ids"] == ", ".join(f"`{m}`" for m in mismatches)
    assert artifact["matched"] + len(mismatches) == artifact["total"]


def test_accuracy_is_not_reported_without_the_failures_that_produced_it(artifact):
    """
    `19 of 25 with the six explained is worth more than a claimed 25 of 25` is
    the eval's stated principle. It holds only if every non-match carries enough
    detail to be explained, so an artifact that lost its failure records cannot
    quietly become a cleaner-looking number.
    """
    for record in artifact["results"]:
        if record["outcome"] == "match":
            continue
        assert record["question"], f"{record['id']} has no question recorded"
        assert record["category"], f"{record['id']} has no category recorded"
        assert record["outcome"] in {"mismatch", "refused", "error"}
        if record["outcome"] == "mismatch":
            assert "gold_sample" in record, f"{record['id']} mismatch with no gold sample"
            assert "agent_sample" in record, f"{record['id']} mismatch with no agent sample"


def test_the_demo_url_is_declared_rather_than_asserted(figures):
    """
    README.md read `Live at the URL above, behind the caps` with no URL anywhere
    above it. The deployment state is now a single declared fact that the README
    renders, so the undeployed case prints an explicit marker and the sentence
    cannot outlive the deploy it describes.
    """
    deployment = json.loads((ROOT / "docs" / "deployment.json").read_text(encoding="utf-8"))
    url = deployment["demo_url"]

    if url is None:
        assert "not yet deployed" in figures["demo_callout"].lower()
        assert "not yet deployed" in figures["demo_availability"].lower()
    else:
        assert url.startswith("https://"), "a demo URL must be https"
        assert url in figures["demo_callout"]
        assert "not yet deployed" not in figures["demo_availability"].lower()
