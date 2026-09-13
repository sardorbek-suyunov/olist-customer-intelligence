"""
Design the eval sample, and say plainly what it cannot measure.

    python scripts/eval_sample.py --duckdb-path <pilot>.duckdb --size 200

TWO STRATA, FOR TWO DIFFERENT QUESTIONS
---------------------------------------
Stratifying purely on the model's own labels would measure precision well and
recall not at all: reviews the model missed are, by construction, absent from a
sample chosen from what the model found. So the sample is split.

  RANDOM stratum   drawn without reference to any label. The only place a false
                   negative can appear, so it is the only stratum that supports
                   a recall estimate, and it also gives unbiased prevalence.

  TARGETED stratum drawn from reviews the model labelled with the rarer aspects.
                   Buys precision support on the tail, where a random draw would
                   return one or two positives and nothing conclusive.

The arithmetic is unforgiving and is reported rather than smoothed over. At 200
reviews and roughly 1.3 aspects each there are only ~260 aspect-instances to go
round, and an aspect at 1.4% prevalence contributes about 1.4 instances to a
100-review random stratum. Precision for such an aspect can be estimated;
recall cannot, at any sample size this project is going to hand-adjudicate.

EVAL_MIN_POSITIVES lives in enrichment/taxonomy.py and was fixed before any of
this ran. Aspects that cannot reach it are named, with their achievable counts,
instead of being scored anyway.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment import taxonomy  # noqa: E402
from enrichment.store import MAP, RAW_SCHEMA, RESULTS  # noqa: E402

CORPUS_WITH_TEXT = 40_950  # reviews carrying a comment; see docs/figures.json


def load_labelled(path: Path) -> dict[str, dict]:
    """review_id -> label, joining the map to the results on the stored hash."""
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            f"""select m.review_id, r.aspects, r.sentiment, r.severity, r.no_content
                from {RAW_SCHEMA}.{MAP} m
                join {RAW_SCHEMA}.{RESULTS} r using (content_hash)"""
        ).fetchall()
    finally:
        con.close()
    return {
        r[0]: {"aspects": json.loads(r[1]), "sentiment": r[2], "severity": r[3], "empty": r[4]}
        for r in rows
    }


def scored_aspects(counts: Counter, n: int) -> list[str]:
    """Aspects that survived the pilot threshold, after declared merges."""
    keep = []
    for aspect in taxonomy.ASPECTS:
        if counts.get(aspect.name, 0) >= taxonomy.PILOT_MIN_OCCURRENCES:
            keep.append(aspect.name)
    return keep


def design(labels: dict[str, dict], size: int, random_share: float, seed: int) -> dict:
    import random as rnd

    rng = rnd.Random(seed)
    ids = sorted(labels)
    n_pilot = len(ids)
    counts = Counter(a for row in labels.values() for a in row["aspects"])
    keep = scored_aspects(counts, n_pilot)

    n_random = int(size * random_share)
    n_targeted = size - n_random

    random_ids = set(rng.sample(ids, min(n_random, len(ids))))

    # Targeted: fill DEFICITS, not equal quotas.
    #
    # The first version handed every aspect the same share of the targeted
    # stratum. That wasted slots on aspects the random stratum had already
    # covered while starving the rare ones, and left five aspects unscored with
    # the corpus holding plenty of examples of each. The deficit is what matters:
    # how many positives an aspect still needs to reach the floor.
    #
    # Greedy set-cover over the deficits. Each pick is the review that closes the
    # most remaining need, so co-occurrence is exploited rather than merely hoped
    # for -- one review carrying three short aspects is worth three carrying one.
    remaining = [i for i in ids if i not in random_ids]
    rng.shuffle(remaining)

    floor = taxonomy.EVAL_MIN_POSITIVES
    deficit = {
        a: max(0, floor - sum(1 for i in random_ids if a in labels[i]["aspects"])) for a in keep
    }

    targeted: set[str] = set()
    pool = [(i, set(labels[i]["aspects"]) & set(keep)) for i in remaining]
    pool = [(i, a) for i, a in pool if a]

    while len(targeted) < n_targeted and any(deficit.values()):
        best, best_gain = None, 0
        for review_id, aspects in pool:
            if review_id in targeted:
                continue
            gain = sum(1 for a in aspects if deficit.get(a, 0) > 0)
            if gain > best_gain:
                best, best_gain = review_id, gain
                if gain == len(deficit):
                    break
        if best is None:
            break
        targeted.add(best)
        for a in dict(pool)[best]:
            if deficit.get(a, 0) > 0:
                deficit[a] -= 1

    # Any slots left over go to a plain random top-up, so the targeted stratum
    # does not quietly become smaller than advertised.
    if len(targeted) < n_targeted:
        for review_id in remaining:
            if len(targeted) >= n_targeted:
                break
            targeted.add(review_id)

    selected = random_ids | targeted
    return {
        "counts": counts,
        "keep": keep,
        "n_pilot": n_pilot,
        "random_ids": random_ids,
        "targeted": targeted,
        "selected": selected,
        "n_random": n_random,
        "n_targeted": n_targeted,
    }


def report(plan: dict, labels: dict[str, dict], size: int) -> None:
    counts, keep, n_pilot = plan["counts"], plan["keep"], plan["n_pilot"]
    random_ids, selected = plan["random_ids"], plan["selected"]

    print(f"Pilot: {n_pilot:,} labelled texts, {len(keep)} aspects above the pilot threshold")
    print(
        f"Eval sample: {len(selected)} reviews "
        f"({len(random_ids)} random + {len(plan['targeted'])} targeted)\n"
    )

    print(f"  {'aspect':<24}{'prev%':>7}{'random':>8}{'targeted':>10}{'total':>7}   verdict")
    floor = taxonomy.EVAL_MIN_POSITIVES
    unscorable_recall, unscorable_at_all = [], []

    for aspect in keep:
        prevalence = counts[aspect] / n_pilot
        in_random = sum(1 for i in random_ids if aspect in labels[i]["aspects"])
        in_total = sum(1 for i in selected if aspect in labels[i]["aspects"])
        in_targeted = in_total - in_random

        if in_total < floor:
            verdict = f"NOT SCORED (< {floor})"
            unscorable_at_all.append(aspect)
        elif in_random < floor:
            verdict = "precision only"
            unscorable_recall.append(aspect)
        else:
            verdict = "precision + recall"
        print(
            f"  {aspect:<24}{prevalence * 100:>6.1f}%{in_random:>8}{in_targeted:>10}"
            f"{in_total:>7}   {verdict}"
        )

    print(
        f"\n  Aspect-instances available in {size} reviews at "
        f"{sum(len(r['aspects']) for r in labels.values()) / n_pilot:.2f} per review: "
        f"~{int(size * sum(len(r['aspects']) for r in labels.values()) / n_pilot)}"
    )
    print(f"  Quotas needed for {len(keep)} aspects at {floor} positives each: {len(keep) * floor}")
    if len(keep) * floor > size * sum(len(r["aspects"]) for r in labels.values()) / n_pilot:
        print("  -> The sample cannot support every aspect. Co-occurrence helps; it does")
        print("     not close a gap this size. The shortfalls are named above.")

    if unscorable_recall:
        print(
            f"\n  RECALL NOT MEASURABLE ({len(unscorable_recall)}): {', '.join(unscorable_recall)}"
        )
        print("    A false negative can only appear in the random stratum, and these are")
        print("    too rare to show up there. Precision is reportable; recall is not.")
    if unscorable_at_all:
        print(f"\n  NOT SCORED AT ALL ({len(unscorable_at_all)}): {', '.join(unscorable_at_all)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, required=True)
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--random-share", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", type=Path, default=None, help="write selected review ids")
    args = parser.parse_args(argv)

    labels = load_labelled(args.duckdb_path)
    if not labels:
        print("no labelled reviews in that store")
        return 1

    plan = design(labels, args.size, args.random_share, args.seed)
    report(plan, labels, args.size)

    if args.out:
        payload = {
            "size": len(plan["selected"]),
            "random": sorted(plan["random_ids"]),
            "targeted": sorted(plan["targeted"]),
            "seed": args.seed,
        }
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
