"""
Read a pilot run and decide the taxonomy against the threshold set beforehand.

    python scripts/pilot_report.py --duckdb-path <pilot>.duckdb
    python scripts/pilot_report.py --duckdb-path a.duckdb --compare b.duckdb

The threshold lives in enrichment/taxonomy.py and was committed before any
review was labelled, so this script applies a rule rather than choosing one. An
aspect below PILOT_MIN_OCCURRENCES folds into its declared parent, or -- if it
has none -- stays extractable and is excluded from the scored eval, with the
exclusion printed rather than quietly applied.

`--compare` is the batch-size A/B. Two runs over the same reviews at different
batch sizes should produce the same labels; where they do not, the disagreement
is the cost of packing 20 reviews into one context, and it is measured here
rather than assumed either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment import taxonomy  # noqa: E402
from enrichment.store import COST_LOG, QUARANTINE, RAW_SCHEMA, RESULTS  # noqa: E402


def read_run(path: Path) -> dict:
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            f"select content_hash, aspects, sentiment, severity, no_content "
            f"from {RAW_SCHEMA}.{RESULTS}"
        ).fetchall()
        quarantined = con.execute(
            f"select reason, count(*) from {RAW_SCHEMA}.{QUARANTINE} group by reason"
        ).fetchall()
        cost = con.execute(
            f"""select coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),
                       coalesce(sum(cost_usd),0), coalesce(sum(wall_seconds),0),
                       max(batch_size), max(model)
                from {RAW_SCHEMA}.{COST_LOG} where reviews > 0"""
        ).fetchone()
    finally:
        con.close()

    return {
        "labels": {
            r[0]: {"aspects": json.loads(r[1]), "sentiment": r[2], "severity": r[3], "empty": r[4]}
            for r in rows
        },
        "quarantine": dict(quarantined),
        "input_tokens": cost[0],
        "output_tokens": cost[1],
        "cost_usd": cost[2],
        "wall_seconds": cost[3],
        "batch_size": cost[4],
        "model": cost[5],
    }


def report(run: dict) -> Counter:
    labels = run["labels"]
    n = len(labels)
    counts = Counter(a for row in labels.values() for a in row["aspects"])
    quarantined = sum(run["quarantine"].values())

    print(f"model        {run['model']}   batch size {run['batch_size']}")
    print(f"labelled     {n:,} distinct texts")
    print(f"quarantined  {quarantined}" + (f"  {run['quarantine']}" if quarantined else ""))
    print(
        f"tokens       {run['input_tokens']:,} in / {run['output_tokens']:,} out"
        f"   ${run['cost_usd']:.4f} standard   {run['wall_seconds']:.0f}s"
    )
    if n:
        print(f"per review   {run['output_tokens'] / n:.1f} output tokens")

    print(f"\nAspect frequencies (threshold {taxonomy.PILOT_MIN_OCCURRENCES} of {n:,}):\n")
    print(f"  {'aspect':<26} {'n':>6} {'%':>7}   verdict")
    below: list[str] = []
    for aspect in taxonomy.ASPECTS:
        hits = counts.get(aspect.name, 0)
        share = hits / n * 100 if n else 0.0
        if hits >= taxonomy.PILOT_MIN_OCCURRENCES:
            verdict = "stands alone"
        elif aspect.parent:
            verdict = f"MERGE -> {aspect.parent}"
            below.append(aspect.name)
        else:
            verdict = "EXCLUDE from scored eval (no parent)"
            below.append(aspect.name)
        print(f"  {aspect.name:<26} {hits:>6,} {share:>6.2f}%   {verdict}")

    empty = sum(1 for r in labels.values() if r["empty"])
    none_assigned = sum(1 for r in labels.values() if not r["aspects"])
    print(f"\n  no_content=true    {empty:,} ({empty / n:.1%})" if n else "")
    print(f"  zero aspects       {none_assigned:,} ({none_assigned / n:.1%})" if n else "")

    print("\n  sentiment:", dict(Counter(r["sentiment"] for r in labels.values())))
    print("  severity :", dict(sorted(Counter(r["severity"] for r in labels.values()).items())))

    if below:
        print(f"\n  {len(below)} aspect(s) below threshold: {', '.join(below)}")
    return counts


def compare(a: dict, b: dict) -> None:
    """Batch-size A/B over whatever the two runs share."""
    shared = set(a["labels"]) & set(b["labels"])
    print(f"\n{'=' * 72}\nA/B: batch {a['batch_size']} vs batch {b['batch_size']}")
    print(f"{len(shared):,} texts labelled by both\n")
    if not shared:
        print("  no overlap -- nothing to compare")
        return

    exact = sentiment_same = severity_same = 0
    jaccard_total = 0.0
    disagreements: Counter = Counter()

    for key in shared:
        left, right = a["labels"][key], b["labels"][key]
        sl, sr = set(left["aspects"]), set(right["aspects"])
        if sl == sr:
            exact += 1
        else:
            for aspect in sl ^ sr:
                disagreements[aspect] += 1
        union = sl | sr
        jaccard_total += (len(sl & sr) / len(union)) if union else 1.0
        sentiment_same += left["sentiment"] == right["sentiment"]
        severity_same += left["severity"] == right["severity"]

    n = len(shared)
    print(f"  exact aspect-set agreement  {exact / n:.1%}  ({exact:,}/{n:,})")
    print(f"  mean Jaccard over aspects   {jaccard_total / n:.3f}")
    print(f"  sentiment agreement         {sentiment_same / n:.1%}")
    print(f"  severity agreement          {severity_same / n:.1%}")
    if disagreements:
        print("\n  aspects most often disputed:")
        for aspect, count in disagreements.most_common(8):
            print(f"    {aspect:<26} {count:>5} ({count / n:.1%} of shared)")

    # Is one arm systematically thinner than the other? That is what a position
    # or attention effect from packing 20 reviews into one context would look
    # like, and it is a different question from whether the two arms agree.
    mean_a = sum(len(a["labels"][k]["aspects"]) for k in shared) / n
    mean_b = sum(len(b["labels"][k]["aspects"]) for k in shared) / n
    only_a = sum(
        len(set(a["labels"][k]["aspects"]) - set(b["labels"][k]["aspects"])) for k in shared
    )
    only_b = sum(
        len(set(b["labels"][k]["aspects"]) - set(a["labels"][k]["aspects"])) for k in shared
    )
    print(f"\n  mean aspects/review   batch {a['batch_size']}: {mean_a:.3f}", end="")
    print(f"   batch {b['batch_size']}: {mean_b:.3f}   delta {mean_b - mean_a:+.3f}")
    print(f"  aspect-instances only in one arm   {only_a} vs {only_b}")
    print("  (symmetric counts and a delta near zero mean label noise, not degradation)")

    # Per REVIEW, always. Totals compare runs of different sizes and say nothing
    # -- the first version of this printed 2.9x where the real input ratio is
    # 10.8x, because one arm covered 1,866 reviews and the other 500.
    print("\n  per review:")
    for run, count in ((a, len(a["labels"])), (b, len(b["labels"]))):
        if not count:
            continue
        print(
            f"    batch {run['batch_size']:>2}  input {run['input_tokens'] / count:7.1f}"
            f"  output {run['output_tokens'] / count:6.1f}"
            f"  ${run['cost_usd'] / count * 1000:6.3f}/1k"
            f"  {run['wall_seconds'] / count:5.2f}s"
        )
    print(
        f"  quarantine    batch {a['batch_size']}: {sum(a['quarantine'].values())}"
        f"   batch {b['batch_size']}: {sum(b['quarantine'].values())}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, required=True)
    parser.add_argument("--compare", type=Path, default=None)
    args = parser.parse_args(argv)

    primary = read_run(args.duckdb_path)
    report(primary)

    if args.compare:
        compare(primary, read_run(args.compare))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
