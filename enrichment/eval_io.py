"""
The eval sample file, defined once.

`scripts/eval_sample.py` writes it and `enrichment.enrich` reads it back to label
exactly those reviews with a reference model. That is two consumers of one shape,
so the shape lives here rather than as a pair of matching dictionary literals --
the same reason the aspect list lives in `taxonomy.py`.

The two strata are kept separate in the file rather than merged into one id list.
They answer different questions (see `scripts/eval_sample.py`), and the scorer
needs to know which stratum a review came from: recall is only estimable on the
random one. Flattening them here would destroy that and the loss would not show
up until someone reported a recall number that quietly included targeted rows.
"""

from __future__ import annotations

import json
from pathlib import Path

RANDOM = "random"
TARGETED = "targeted"


def write_sample(path: Path, random_ids: set[str], targeted: set[str], seed: int) -> None:
    payload = {
        "size": len(random_ids | targeted),
        RANDOM: sorted(random_ids),
        TARGETED: sorted(targeted),
        "seed": seed,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_sample(path: Path) -> dict[str, list[str]]:
    """{'random': [...], 'targeted': [...]} -- strata preserved."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in (RANDOM, TARGETED) if k not in payload]
    if missing:
        raise ValueError(f"{path} is not an eval sample file: missing {missing}")
    return {RANDOM: list(payload[RANDOM]), TARGETED: list(payload[TARGETED])}


def read_sample_ids(path: Path) -> set[str]:
    """Every review id in the sample, both strata, for the labelling pass."""
    strata = read_sample(path)
    return set(strata[RANDOM]) | set(strata[TARGETED])
