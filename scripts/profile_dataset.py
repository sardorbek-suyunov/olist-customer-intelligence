"""
Reproducible profiling of the Olist source data.

Every quantitative claim in README.md is produced by this script. Run it with
`make profile`; it writes docs/data_profiling.md and docs/figures.json. If the
source data changes, both change, and the claims stay honest.

figures.json is what makes that true of the README rather than just of this
report. README.md is rendered from README.template.md against those figures
(`make readme`), so a number in the README cannot disagree with the data -- there
is no hand-copied second value to disagree with. CI fails if either output is
stale.

The headline findings it exists to establish:
  1. customer_id is order-scoped and never repeats -> dbt snapshot cannot work
  2. products and sellers have one observation per key -> no SCD2 is derivable
  3. products has no price column -> a price-based SCD2 rationale is false
  4. normalization removes 0 spurious version boundaries on customers,
     but collapses 8 genuine city variants on sellers
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "archive"
OUT = ROOT / "docs" / "data_profiling.md"
FIGURES_OUT = ROOT / "docs" / "figures.json"
PARITY_SEED = ROOT / "transform" / "seeds" / "normalization_parity.csv"

SCD_ATTRIBUTES = ["customer_zip_code_prefix", "customer_city", "customer_state"]

# Standalone spacing accents that act as punctuation in this data. Must match
# spacing_accent_class() in transform/macros/normalize_text.sql exactly.
SPACING_ACCENTS = re.compile("[¨¯´¸ˆ˜]")


# Every distinct location string in the source, used to prove that this
# module's normalize() and the normalize_text dbt macro agree exactly.
PARITY_COLUMNS = [
    ("customers", "customer_city"),
    ("customers", "customer_state"),
    ("sellers", "seller_city"),
    ("sellers", "seller_state"),
    ("geolocation", "geolocation_city"),
    ("geolocation", "geolocation_state"),
]


FIGURES: dict[str, object] = {}


def fig(name: str, value):
    """
    Record a figure under `name` and return it unchanged.

    Used inline at the point of computation so the number written to
    figures.json is the same object the markdown prints. A separate "collect the
    figures" pass would be a second derivation of the same quantities, which is
    the shape of every drift bug this file exists to prevent.
    """
    FIGURES[name] = value.item() if hasattr(value, "item") else value
    return FIGURES[name]


def strip_accents(value: str) -> str:
    """
    The part of normalization that accounts for legitimate Portuguese accents.

    Factored out because the mojibake count needs exactly this and nothing more:
    whatever is still non-ASCII *after* accents are resolved is a character no
    accent explains. Recomputing that separately is how the counts in the README
    drifted from the data in the first place.
    """
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return SPACING_ACCENTS.sub(" ", stripped)


def has_mojibake(value: object) -> bool:
    """
    True for a non-ASCII character that accent-stripping cannot account for.

    'são paulo' is not mojibake: NFD splits 'ã' into 'a' plus a combining tilde
    and the tilde drops out. 'sa£o paulo' is: the '£' is a bare Latin-1
    continuation byte left behind when double-encoded UTF-8 was accent-stripped,
    and nothing decomposes it away.
    """
    return not pd.isna(value) and not strip_accents(str(value)).isascii()


def normalize(value: object) -> object:
    """
    Python half of the normalization defined in macros/normalize_text.sql.

    NFD (canonical), never NFKD: compatibility decomposition would rewrite
    '4º centenario' to '4o centenario' and 'maceia³' to 'maceia3',
    inventing characters the name never had.

    Standalone spacing accents (U+00B4 ACUTE and friends) become a SPACE
    before the ascii-ignore encode deletes everything else non-ASCII. Without
    that step 'santa barbara d´oeste' collapses to '...doeste', which
    fails to merge with the correctly spelled "d'oeste" and splits one seller
    city into two. Other non-ASCII is DELETED; surviving punctuation becomes a
    space. See the macro docstring for why the asymmetry is deliberate.

    assert_normalize_macro_matches_python proves this stays identical to the
    SQL implementation in the target's own dialect.
    """
    if pd.isna(value):
        return value
    ascii_only = strip_accents(str(value)).encode("ascii", "ignore").decode()
    folded = re.sub(r"[^a-z0-9 ]+", " ", ascii_only.lower())
    return re.sub(r"\s+", " ", folded).strip() or None


def load() -> dict[str, pd.DataFrame]:
    names = [
        "customers",
        "orders",
        "order_items",
        "order_payments",
        "order_reviews",
        "products",
        "sellers",
        "geolocation",
    ]
    frames = {n: pd.read_csv(RAW / f"olist_{n}_dataset.csv") for n in names}
    frames["orders"]["order_purchase_timestamp"] = pd.to_datetime(
        frames["orders"]["order_purchase_timestamp"]
    )
    return frames


def write_parity_seed(frames: dict[str, pd.DataFrame]) -> int:
    """
    Emit every distinct location string with its Python-normalized form.

    normalize() above and the normalize_text dbt macro implement the same
    transformation in two languages. Rather than asking a docstring to keep
    them in sync, this seed is checked by
    tests/assert_normalize_macro_matches_python.sql on every `dbt build` --
    in the real adapter dialect, so the duckdb and bigquery implementations
    are each verified against the same reference.
    """
    rows: list[dict[str, object]] = []
    for table, column in PARITY_COLUMNS:
        for value in frames[table][column].dropna().unique():
            rows.append(
                {
                    "source_column": f"{table}.{column}",
                    "raw_value": value,
                    "python_normalized": normalize(value),
                }
            )

    frame = pd.DataFrame(rows).drop_duplicates(subset=["source_column", "raw_value"])
    frame = frame.sort_values(["source_column", "raw_value"]).reset_index(drop=True)
    PARITY_SEED.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(PARITY_SEED, index=False, encoding="utf-8")
    return len(frame)


def version_boundaries(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    """Consecutive-row change detection within each customer, time-ordered."""
    previous = frame.groupby("customer_unique_id")[columns].shift(1)
    changed = pd.Series(False, index=frame.index)
    for column in columns:
        changed |= previous[column].notna() & (previous[column] != frame[column])
    return changed


def main() -> None:
    f = load()
    customers, orders, sellers = f["customers"], f["orders"], f["sellers"]
    lines: list[str] = []
    w = lines.append

    w("# Data profiling")
    w("")
    w("> Generated by `scripts/profile_dataset.py`. Do not edit by hand.")
    w("")

    # ---- 1. grain -------------------------------------------------------
    w("## 1. Key grain")
    w("")
    w("| Table | Rows | Distinct key | Repeat observations |")
    w("|---|---:|---:|---:|")
    for name, key in [
        ("customers", "customer_id"),
        ("orders", "order_id"),
        ("products", "product_id"),
        ("sellers", "seller_id"),
    ]:
        df = f[name]
        rows = fig(f"grain_{name}_rows", len(df))
        distinct = fig(f"grain_{name}_distinct", df[key].nunique())
        repeats = fig(f"grain_{name}_repeats", rows - distinct)
        w(f"| `{name}` | {rows:,} | {distinct:,} | {repeats:,} |")
    w("")
    w(
        f"- `orders.customer_id` distinct: **{orders.customer_id.nunique():,}** of {len(orders):,} rows -> strictly 1:1 with `order_id`."
    )
    w(f"- `customer_unique_id` distinct: **{customers.customer_unique_id.nunique():,}**")
    repeat_counts = customers.customer_unique_id.value_counts()
    w(
        f"- Customers with more than one order: **{int((repeat_counts > 1).sum()):,}** (max {int(repeat_counts.max())} orders)"
    )
    w("")
    w("**Consequence.** `customer_id` is an order-scoped surrogate. A `dbt snapshot` keyed")
    w("on it emits one version per row and zero change events. SCD2 must be derived at")
    w("`customer_unique_id` grain instead.")
    w("")
    w("**Consequence.** `products` and `sellers` have exactly one observation per key, so")
    w("no snapshot strategy can produce a second version there. Both are Type 1 by necessity.")
    w("")
    w("- `products` columns: `{}`".format("`, `".join(f["products"].columns)))
    w(
        "  There is **no price column**; `price` and `freight_value` are attributes of `order_items`."
    )
    w("")

    # ---- 2. scd2 --------------------------------------------------------
    obs = customers.merge(
        orders[["customer_id", "order_purchase_timestamp"]], on="customer_id", how="inner"
    )
    assert len(obs) == len(customers), "customers/orders join is not 1:1"
    obs["city_n"] = obs.customer_city.map(normalize)
    obs["state_n"] = obs.customer_state.map(normalize)
    obs = obs.sort_values(["customer_unique_id", "order_purchase_timestamp", "customer_id"])

    raw_changes = version_boundaries(obs, SCD_ATTRIBUTES)
    norm_changes = version_boundaries(obs, ["customer_zip_code_prefix", "city_n", "state_n"])
    n_versions = fig("scd2_change_events", int(norm_changes.sum()))
    n_current = fig("dim_customers_current", customers.customer_unique_id.nunique())
    fig("dim_customers_historical", n_versions)
    fig("dim_customers_rows", n_current + n_versions)

    w("## 2. Derived SCD2 at `customer_unique_id` grain")
    w("")
    w(
        "- Change events: **{}** across **{}** customers".format(
            n_versions,
            fig("scd2_customers_changed", obs.loc[norm_changes, "customer_unique_id"].nunique()),
        )
    )
    w(
        f"- `dim_customers` rows: {n_current:,} current + {n_versions} historical = **{n_current + n_versions:,}**"
    )
    for label, cols in [
        ("zip", ["customer_zip_code_prefix"]),
        ("city", ["city_n"]),
        ("state", ["state_n"]),
    ]:
        w(f"  - {label} changes: {int(version_boundaries(obs, cols).sum())}")
    w("")

    ties = obs.groupby(["customer_unique_id", "order_purchase_timestamp"]).size()
    w(
        f"- Tied `(customer, timestamp)` pairs: **{fig('tied_pairs', int((ties > 1).sum()))}** "
        f"covering {int(ties[ties > 1].sum())} rows. Requires a"
    )
    w("  deterministic tiebreaker (`customer_id`) and half-open `[valid_from, valid_to)`")
    w("  intervals to avoid fan-out.")
    w("")

    # ---- 3. normalization -----------------------------------------------
    punct = customers[customers.customer_city.str.contains(r"[^a-z0-9 ]", na=False)]
    seller_norm = sellers.seller_city.map(normalize)

    w("## 3. Does normalization do anything?")
    w("")
    w(
        f"- Customers, version boundaries raw vs normalized: "
        f"**{int(raw_changes.sum())} vs {n_versions}** -> "
        f"**{fig('spurious_versions_removed', int(raw_changes.sum()) - n_versions)} spurious versions removed**"
    )
    w(
        f"- ...but it rewrites **{fig('customer_punct_rows', len(punct))}** rows across "
        f"**{fig('customer_punct_cities', punct.customer_city.nunique())}** legitimately punctuated city names"
    )
    w(
        "  (`{}`). The dimension therefore diffs on the normalized".format(
            "`, `".join(sorted(punct.customer_city.unique())[:3])
        )
    )
    w("  value and **displays the raw one**.")
    w(
        f"- Sellers, distinct cities raw vs normalized: "
        f"**{fig('seller_cities_raw', sellers.seller_city.nunique())} vs "
        f"{fig('seller_cities_normalized', seller_norm.nunique())}** -> "
        f"**{fig('seller_cities_merged', sellers.seller_city.nunique() - seller_norm.nunique())} "
        f"genuine variants merged**."
    )
    w("  Normalization is load-bearing here.")
    w("")
    w("| Raw variants | Canonical |")
    w("|---|---|")
    for canonical, group in sellers.assign(_n=seller_norm).groupby("_n"):
        variants = sorted(group.seller_city.unique())
        if len(variants) > 1:
            w("| `{}` | `{}` |".format("`, `".join(variants), canonical))
    w("")

    # ---- 4. impact ------------------------------------------------------
    current = obs.groupby("customer_unique_id")[SCD_ATTRIBUTES].last()
    joined = obs.join(current, on="customer_unique_id", rsuffix="_current")
    wrong = pd.Series(False, index=joined.index)
    for column in SCD_ATTRIBUTES:
        wrong |= joined[column] != joined[column + "_current"]
    wrong_state = joined.customer_state != joined.customer_state_current

    w("## 4. Cost of getting the join wrong")
    w("")
    w("Joining `fct_orders` to the **current** dimension row instead of the version valid")
    w(
        "at purchase time mis-attributes **{} of {:,} orders ({:.3f}%)** across **{}**".format(
            fig("misattributed_orders", int(wrong.sum())),
            fig("orders_total", len(joined)),
            fig("misattributed_pct", round(100 * wrong.mean(), 3)),
            fig("misattributed_customers", joined.loc[wrong, "customer_unique_id"].nunique()),
        )
    )
    w(
        f"customers, **{fig('misattributed_wrong_state', int(wrong_state.sum()))}** of them to the wrong state."
    )
    w("")

    parity_rows = fig("parity_rows", write_parity_seed(f))
    w("## 5. Cross-language normalization parity")
    w("")
    w("`scripts/profile_dataset.py::normalize()` and the `normalize_text` dbt macro must")
    w(f"agree exactly. `transform/seeds/normalization_parity.csv` holds all **{parity_rows:,}**")
    w("distinct location strings in the source with their Python-normalized form;")
    w("`assert_normalize_macro_matches_python` re-derives each one through the macro in")
    w("the target's own SQL dialect and fails the build on any disagreement.")
    w("")

    # ---- 6. mojibake ----------------------------------------------------
    w("## 6. Non-ASCII survivors in the source")
    w("")
    w("Two different things get called mojibake, so both are counted. *Non-ASCII* is any")
    w("character outside 7-bit ASCII, which includes every legitimately accented city")
    w("name. *Mojibake* is the subset that survives accent-stripping -- a character no")
    w("accent explains, such as the bare Latin-1 continuation byte left behind when")
    w("double-encoded UTF-8 was accent-stripped.")
    w("")
    w("Only the second column is a data-quality problem, and it is the one the decision")
    w("to document rather than repair rests on.")
    w("")
    w("| Source column | Non-ASCII rows | distinct | Mojibake rows | distinct |")
    w("|---|---:|---:|---:|---:|")
    for frame, column, key in [
        ("customers", "customer_city", "customers"),
        ("sellers", "seller_city", "sellers"),
        ("geolocation", "geolocation_city", "geolocation"),
    ]:
        values = f[frame][column].fillna("")
        raw = values[values.map(lambda v: not v.isascii())]
        bad = values[values.map(has_mojibake)]
        w(
            "| `{}.{}` | {} | {} | {} | {} |".format(
                frame,
                column,
                fig(f"non_ascii_{key}_rows", len(raw)),
                fig(f"non_ascii_{key}_distinct", raw.nunique()),
                fig(f"mojibake_{key}_rows", len(bad)),
                fig(f"mojibake_{key}_distinct", bad.nunique()),
            )
        )
    w("")
    fig("source_rows_total", sum(len(frame) for frame in f.values()))
    fig(
        "mojibake_rows_total",
        sum(int(FIGURES[f"mojibake_{k}_rows"]) for k in ("customers", "sellers", "geolocation")),
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    FIGURES_OUT.write_text(json.dumps(FIGURES, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"wrote {FIGURES_OUT} ({len(FIGURES)} figures)")
    print(f"wrote {PARITY_SEED} ({parity_rows:,} rows)")
    print(
        f"  scd2_versions={n_versions}  spurious_removed={int(raw_changes.sum()) - n_versions}  misattributed={int(wrong.sum())}"
    )


if __name__ == "__main__":
    main()
