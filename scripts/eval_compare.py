"""
Compare two eval score files and show what a prompt change actually did.

    python scripts/eval_compare.py --before enrichment/eval/scores_v1.json \
        --after enrichment/eval/scores_v2.json

The table is the point, not the headline number. A prompt change that lifts one
aspect usually costs something somewhere else, and reporting only the aggregate
hides the trade that was actually made. Regressions are called out separately so
they cannot be lost in an average that improved.

Micro-averages weight by support and macro-averages do not. Both are printed
because they disagree in a way that matters here: the corpus is dominated by a
few common aspects, so a micro-average largely reports what happened to those,
while a macro-average gives a 30-support aspect the same vote as a 167-support
one. Neither is the right one; reading only one of them is the mistake.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def totals(scores: dict) -> dict:
    aspects = scores["aspects"]
    tp = sum(a["tp"] for a in aspects.values())
    fp = sum(a["fp"] for a in aspects.values())
    fn = sum(a["fn"] for a in aspects.values())
    micro_p = tp / (tp + fp) if (tp + fp) else 0.0
    micro_r = tp / (tp + fn) if (tp + fn) else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
    precisions = [a["precision"] for a in aspects.values() if a["precision"] is not None]
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "micro_precision": micro_p,
        "micro_recall": micro_r,
        "micro_f1": micro_f1,
        "macro_precision": sum(precisions) / len(precisions) if precisions else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))

    print(
        f"before  {before['subject']} @ {before.get('subject_version', '?')}"
        f"   after  {after['subject']} @ {after.get('subject_version', '?')}"
    )
    print(
        f"reference {after['reference']} @ {after.get('reference_version', '?')}, "
        "unchanged across the iteration\n"
    )

    header = (
        f"  {'aspect':<24}{'sup':>5}{'FN':>9}{'FP':>9}{'precision':>20}{'recall':>18}{'dF1':>9}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    regressions, improvements = [], []
    for name, b in before["aspects"].items():
        a = after["aspects"][name]

        def pair(x: float | None, y: float | None, width: int) -> str:
            if x is None and y is None:
                return f"{'--':>{width}}"
            sx = f"{x:.3f}" if x is not None else "--"
            sy = f"{y:.3f}" if y is not None else "--"
            return f"{sx + ' -> ' + sy:>{width}}"

        d_f1 = a["f1"] - b["f1"] if a["f1"] is not None and b["f1"] is not None else None
        print(
            f"  {name:<24}{b['support']:>5}"
            f"{f'{b["fn"]} -> {a["fn"]}':>9}{f'{b["fp"]} -> {a["fp"]}':>9}"
            f"{pair(b['precision'], a['precision'], 20)}"
            f"{pair(b['recall'], a['recall'], 18)}"
            f"{f'{d_f1:+.3f}' if d_f1 is not None else '--':>9}"
        )

        if b["precision"] is not None and a["precision"] is not None:
            delta = a["precision"] - b["precision"]
            moved = (a["fp"] - b["fp"]) + (a["fn"] - b["fn"])
            if delta < -0.02:
                regressions.append((name, b["precision"], a["precision"], moved))
            elif delta > 0.02:
                improvements.append((name, b["precision"], a["precision"], moved))

    tb, ta = totals(before), totals(after)
    print(f"\n  {'':<24}{'before':>10}{'after':>10}{'delta':>10}")
    for key, label in (
        ("fn", "false negatives"),
        ("fp", "false positives"),
        ("micro_precision", "micro precision"),
        ("micro_recall", "micro recall"),
        ("micro_f1", "micro F1"),
        ("macro_precision", "macro precision"),
    ):
        x, y = tb[key], ta[key]
        fmt = "{:>10.3f}" if isinstance(x, float) else "{:>10}"
        delta = f"{y - x:+.3f}" if isinstance(x, float) else f"{y - x:+d}"
        print(f"  {label:<24}" + fmt.format(x) + fmt.format(y) + f"{delta:>10}")

    for label, rows in (("REGRESSED", regressions), ("IMPROVED", improvements)):
        if rows:
            print(f"\n  {label} (precision moved more than 2pp):")
            for name, x, y, moved in sorted(rows, key=lambda r: r[2] - r[1]):
                print(f"    {name:<24}{x:.3f} -> {y:.3f}   ({moved:+d} errors)")

    for key, label in (
        ("exact_match", "exact aspect-set match"),
        ("sentiment_agreement", "sentiment agreement"),
        ("severity_agreement", "severity agreement"),
    ):
        x, y, n = before[key], after[key], after["scored"]
        print(f"\n  {label:<24}{x}/{n} ({x / n:.1%})  ->  {y}/{n} ({y / n:.1%})")

    if args.out:
        args.out.write_text(json.dumps({"before": tb, "after": ta}, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
