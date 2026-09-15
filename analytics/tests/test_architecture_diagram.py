"""
Every model and dataset named in the architecture diagram must exist.

A diagram is the easiest thing in a repository to let rot. It is not compiled,
not imported, and not asserted by anything, so it goes on describing whatever was
true the day it was drawn -- and unlike a stale number, a stale box looks exactly
like a correct one. An exported image is worse again: it cannot even be diffed,
so a reviewer sees `architecture.png | Bin 214382 -> 219117 bytes` and moves on.

That is the whole reason docs/architecture.mmd is the source of truth and the
image is rendered from it. The text is diffable, and this test makes it falsifiable:
rename a model, drop one, move a dataset, and the diagram fails the build instead
of quietly lying. Without this, an image artifact would not earn its place here.

WHY THE IMAGE IS AN SVG
-----------------------
Because an SVG is text, and that turns the last unverifiable link into a checked
one. The usual gap with a generated diagram is that the source is validated and
the exported picture beside it is not, so the two drift and only the unchecked
one is on display. Here the rendered SVG still contains the label text, so this
file can assert that the image was rendered from THIS source rather than an older
one -- which a PNG could never support.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIAGRAM = ROOT / "docs" / "architecture.mmd"
MANIFEST = ROOT / "transform" / "target" / "manifest.json"
IMAGE = ROOT / "docs" / "img" / "architecture.svg"

# Prefixes that name a dbt model in this project's conventions. Anything in the
# diagram matching one of these is claimed to be a model, so it has to be one.
MODEL_PREFIXES = ("stg_", "int_", "dim_", "fct_")

# Datasets the diagram may name. Kept as a literal set rather than read from
# profiles.yml, because profiles.yml resolves them from env vars that are absent
# in CI -- and a test that silently compares '' to '' is the empty-table ceiling
# check all over again.
KNOWN_DATASETS = {"olist_raw", "olist_staging", "olist_marts", "olist_enrichment"}


@pytest.fixture(scope="module")
def diagram() -> str:
    assert DIAGRAM.exists(), f"{DIAGRAM} is missing; it is the diagram's source of truth"
    return DIAGRAM.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def manifest() -> dict:
    if not MANIFEST.exists():
        pytest.skip(f"{MANIFEST} not found; run `make build` to check the diagram")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def models(manifest) -> dict[str, dict]:
    return {
        node["name"]: node
        for node in manifest["nodes"].values()
        if node["resource_type"] == "model"
    }


def named_models(text: str) -> set[str]:
    """Model-shaped identifiers in the diagram, ignoring comment lines."""
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("%%"))
    pattern = r"\b(" + "|".join(MODEL_PREFIXES) + r")[a-z0-9_]+\b"
    return {m.group(0) for m in re.finditer(pattern, body)}


def test_the_diagram_names_models_that_exist(diagram, models):
    """The assertion the .mmd exists to make possible."""
    drawn = named_models(diagram)
    assert drawn, "the diagram names no dbt models at all -- has its notation changed?"

    missing = sorted(drawn - set(models))
    assert not missing, (
        f"docs/architecture.mmd names models that are not in the dbt manifest: {missing}.\n"
        "Either the model was renamed or dropped and the diagram was not updated, or the "
        "diagram has a typo. Both make the picture wrong; fix the .mmd, then `make diagram`."
    )


def test_every_mart_appears_in_the_diagram(diagram, models):
    """
    The reverse direction, for marts only.

    A diagram that omits a model is not wrong the way a diagram that invents one
    is, so this is deliberately not applied to staging. The marts are the
    project's public surface -- they are what the agent queries and what the
    dashboard reads -- and a mart missing from the architecture picture is a gap
    in the thing the picture is for.
    """
    marts = {name for name, node in models.items() if node["config"].get("schema") == "marts"}
    drawn = named_models(diagram)
    missing = sorted(marts - drawn)
    assert not missing, (
        f"these marts exist but are not in docs/architecture.mmd: {missing}. "
        "The diagram is the map of what this project serves; a mart absent from it "
        "is invisible to every reader who starts there."
    )


def test_the_diagram_names_datasets_that_are_real(diagram):
    drawn = set(re.findall(r"\bolist_[a-z_]+\b", diagram))
    # Names that are project/dataset-shaped but are neither: the repo and the DAGs.
    drawn -= {"olist_batch", "olist_backfill_monthly", "olist_incremental_daily"}
    unknown = sorted(drawn - KNOWN_DATASETS)
    assert not unknown, (
        f"docs/architecture.mmd names datasets that do not exist: {unknown}. "
        f"Known datasets are {sorted(KNOWN_DATASETS)}."
    )


def test_the_dual_adapter_split_is_actually_drawn(diagram):
    """
    The diagram's stated purpose is making the two-warehouse design visible, and
    it does that with node classes rather than words. A refactor that drops the
    classDefs would leave a diagram that still renders, still passes every check
    above, and no longer shows the one thing it was drawn to show.
    """
    for required in ("classDef bq", "classDef duck", "classDef both"):
        assert required in diagram, f"the engine colouring lost {required!r}"

    for engine in ("BigQuery", "DuckDB"):
        assert engine in diagram, f"{engine} is not named anywhere in the diagram"


def test_the_image_exists_alongside_its_source():
    """
    The README embeds the SVG. A committed diagram source with no rendering is a
    broken image in the README, which is the first thing a reader sees.
    """
    assert IMAGE.exists(), (
        f"docs/architecture.mmd exists but {IMAGE.name} does not. Render it:\n  make diagram"
    )
    assert IMAGE.stat().st_size > 10_000, (
        f"{IMAGE} is {IMAGE.stat().st_size} bytes, which is too small to be the "
        "rendered diagram -- a truncated or failed render."
    )


def test_the_image_was_rendered_from_this_source(diagram):
    """
    The link that is usually left to a person, and therefore usually broken.

    A validated source next to an unvalidated export is worth very little: the
    checked artifact and the displayed artifact are different files, and the one
    on display is the one nobody checked. Because the rendering is SVG, the label
    text survives into it, so a picture rendered from an older revision fails
    here instead of sitting in the README describing a pipeline that moved.
    """
    rendered = IMAGE.read_text(encoding="utf-8")
    drawn = named_models(diagram)

    missing = sorted(name for name in drawn if name not in rendered)
    assert not missing, (
        f"{IMAGE.name} does not contain {missing}, which docs/architecture.mmd names. "
        "The image was rendered from an older version of the source. Re-render it:\n"
        "  make diagram"
    )
