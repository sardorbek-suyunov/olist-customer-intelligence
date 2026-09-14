"""
Score the production labels against a stronger model's, per aspect.

    python scripts/eval_score.py --duckdb-path transform/olist.duckdb \
        --sample enrichment/eval/eval_sample_600.json \
        --subject gemini-3.1-flash-lite --reference gemini-3.8-flash

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
Agreement with a stronger model, not accuracy. Where both models share a
misreading this scores it correct, and the ceiling is the reference's own
unmeasured error rate. Usable for comparison -- v1 against v2, aspect against
aspect -- and not as an absolute quality claim. docs/DECISIONS.md, decision 5.

PRECISION AND RECALL COME FROM DIFFERENT STRATA, ON PURPOSE
-----------------------------------------------------------
Recall needs false negatives, and a false negative is a review the subject did
not label. Those can only be found in a sample drawn without reference to the
subject's labels, so recall is computed on the RANDOM stratum alone and only
where that stratum holds at least EVAL_MIN_POSITIVES reference positives.

Precision is computed over both strata, because the targeted stratum was drawn
from the subject's own positives -- which is exactly the population precision is
conditional on. One caveat is reported rather than buried: that draw was a greedy
set-cover over deficits, so it favours reviews carrying several rare aspects at
once. Precision on the tail is therefore an estimate over co-occurrence-heavy
reviews, not over a uniform draw of the subject's positives.

An aspect below the floor is named with its counts. It is never given a number.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrichment import eval_io, taxonomy  # noqa: E402
from enrichment.store import MAP, RAW_SCHEMA, RESULTS  # noqa: E402


def load_two_models(
    path: Path,
    subject: str,
    reference: str,
    subject_version: str | None = None,
    reference_version: str | None = None,
) -> dict[str, dict[str, dict]]:
    """
    review_id -> {model -> label}, joined through the map, not on the hash.

    Each side carries its own prompt version, because the prompt iteration moves
    the subject to v2 while the reference stays where it was. That is deliberate:
    the reference is a standard for what the REVIEWS say, and v2 is an attempt to
    make the subject agree with it, not a redefinition of what counts as correct.
    Re-labelling the reference at v2 as well would move the target and the
    before/after comparison would measure nothing.

    It only holds while a version bump clarifies rather than redefines an aspect.
    A v-next that genuinely changes what an aspect MEANS needs a fresh reference,
    and this is the line to remember that at.
    """
    import duckdb

    subject_version = subject_version or taxonomy.PROMPT_VERSION
    reference_version = reference_version or taxonomy.PROMPT_VERSION

    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            f"""select m.review_id, r.model, r.aspects, r.sentiment, r.severity, r.no_content
                from {RAW_SCHEMA}.{MAP} m
                join {RAW_SCHEMA}.{RESULTS} r using (content_hash)
                where (r.model = ? and r.prompt_version = ?)
                   or (r.model = ? and r.prompt_version = ?)""",
            [subject, subject_version, reference, reference_version],
        ).fetchall()
    finally:
        con.close()

    out: dict[str, dict[str, dict]] = {}
    for review_id, model, aspects, sentiment, severity, empty in rows:
        out.setdefault(review_id, {})[model] = {
            "aspects": set(json.loads(aspects)),
            "sentiment": sentiment,
            "severity": severity,
            "empty": empty,
        }
    return out


def confusion(
    paired: dict[str, dict[str, dict]], ids: set[str], aspect: str, subject: str, reference: str
) -> tuple[int, int, int, int]:
    """(tp, fp, fn, tn) treating the reference as truth."""
    tp = fp = fn = tn = 0
    for review_id in ids:
        both = paired[review_id]
        said = aspect in both[subject]["aspects"]
        truth = aspect in both[reference]["aspects"]
        if said and truth:
            tp += 1
        elif said:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson interval. Normal approximation breaks down exactly where it matters."""
    if n == 0:
        return (0.0, 1.0)
    z = 1.959964
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--subject", required=True, help="the model being scored")
    parser.add_argument("--reference", required=True, help="the stronger model, treated as truth")
    parser.add_argument(
        "--subject-version", default=None, help=f"default {taxonomy.PROMPT_VERSION}"
    )
    parser.add_argument(
        "--reference-version",
        default=None,
        help="the prompt version the reference labels were produced under; leave it "
        "where it was across a prompt iteration so the target does not move",
    )
    parser.add_argument("--out", type=Path, default=None, help="write scores as json")
    args = parser.parse_args(argv)

    strata = eval_io.read_sample(args.sample)
    random_ids, targeted_ids = set(strata["random"]), set(strata["targeted"])
    paired = load_two_models(
        args.duckdb_path,
        args.subject,
        args.reference,
        args.subject_version,
        args.reference_version,
    )

    scored = {i for i in random_ids | targeted_ids if len(paired.get(i, {})) == 2}
    missing = (random_ids | targeted_ids) - scored
    random_scored = random_ids & scored

    sub_v = args.subject_version or taxonomy.PROMPT_VERSION
    ref_v = args.reference_version or taxonomy.PROMPT_VERSION
    print(f"subject   {args.subject} @ {sub_v}")
    print(f"reference {args.reference} @ {ref_v}   (truth; see docs/DECISIONS.md 5 and 5a)")
    print(f"scored    {len(scored)} of {len(random_ids | targeted_ids)} sampled reviews")
    if missing:
        print(f"          {len(missing)} unusable: only one model has a label for them")
    print(f"          {len(random_scored)} in the random stratum, the only recall-bearing one\n")

    floor = taxonomy.EVAL_MIN_POSITIVES
    results: dict[str, dict] = {}
    no_recall: list[str] = []

    header = (
        f"  {'aspect':<24}{'sup':>5}{'TP':>5}{'FP':>5}{'FN':>5}"
        f"{'prec':>7}{'95% CI':>14}{'rec':>7}{'F1':>7}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for aspect in taxonomy.ASPECT_NAMES:
        tp, fp, fn, _ = confusion(paired, scored, aspect, args.subject, args.reference)
        support = tp + fn  # reference positives in the whole sample
        precision = tp / (tp + fp) if (tp + fp) else None
        lo, hi = wilson(tp, tp + fp) if (tp + fp) else (0.0, 1.0)

        # Recall: random stratum only, and only above the floor.
        r_tp, _, r_fn, _ = confusion(paired, random_scored, aspect, args.subject, args.reference)
        r_support = r_tp + r_fn
        if r_support >= floor:
            recall = r_tp / r_support if r_support else None
        else:
            recall = None
            no_recall.append(aspect)

        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and (precision + recall)
            else None
        )

        def fmt(x: float | None, width: int = 7) -> str:
            return f"{x:>{width}.3f}" if x is not None else f"{'--':>{width}}"

        ci = f"[{lo:.2f},{hi:.2f}]" if (tp + fp) else "--"
        print(
            f"  {aspect:<24}{support:>5}{tp:>5}{fp:>5}{fn:>5}"
            f"{fmt(precision)}{ci:>14}{fmt(recall)}{fmt(f1)}"
        )
        results[aspect] = {
            "support": support,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "precision_ci95": [lo, hi],
            "recall": recall,
            "recall_support_random": r_support,
            "f1": f1,
        }

    if no_recall:
        print(f"\n  RECALL NOT REPORTED ({len(no_recall)} of {len(taxonomy.ASPECT_NAMES)}):")
        print(f"    {', '.join(no_recall)}")
        print(f"    Fewer than {floor} reference positives in the {len(random_scored)}-review")
        print("    random stratum. A false negative cannot appear anywhere else, so recall")
        print("    is not estimable at this sample size. Precision above is unaffected.")

    print("\n  Precision on rare aspects leans on the targeted stratum, which was a greedy")
    print("  set-cover over deficits rather than a uniform draw of the subject's positives.")
    print("  It favours reviews carrying several rare aspects at once.")

    # Whole-label agreement, which per-aspect scores can hide.
    exact = sum(
        1
        for i in scored
        if paired[i][args.subject]["aspects"] == paired[i][args.reference]["aspects"]
    )
    sent = sum(
        1
        for i in scored
        if paired[i][args.subject]["sentiment"] == paired[i][args.reference]["sentiment"]
    )
    sev = sum(
        1
        for i in scored
        if paired[i][args.subject]["severity"] == paired[i][args.reference]["severity"]
    )
    n = len(scored) or 1
    print(f"\n  Exact aspect-set match  {exact:>4}/{len(scored)}  {exact / n:.1%}")
    print(f"  Sentiment agreement     {sent:>4}/{len(scored)}  {sent / n:.1%}")
    print(f"  Severity agreement      {sev:>4}/{len(scored)}  {sev / n:.1%}")

    pairs = Counter(
        (paired[i][args.reference]["sentiment"], paired[i][args.subject]["sentiment"])
        for i in scored
    )
    labels = list(taxonomy.SENTIMENTS)
    print(f"\n  Sentiment confusion (rows = {args.reference}, cols = {args.subject})")
    print("    " + "".join(f"{c:>10}" for c in ["ref \\ sub", *labels]))
    for truth in labels:
        row = "".join(f"{pairs.get((truth, got), 0):>10}" for got in labels)
        print(f"    {truth:>10}{row}")

    if args.out:
        payload = {
            "subject": args.subject,
            "reference": args.reference,
            "subject_version": sub_v,
            "reference_version": ref_v,
            "scored": len(scored),
            "random_scored": len(random_scored),
            "eval_min_positives": floor,
            "aspects": results,
            "recall_not_reported": no_recall,
            "exact_match": exact,
            "sentiment_agreement": sent,
            "severity_agreement": sev,
        }
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
