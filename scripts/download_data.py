"""
Fetch the Olist dataset into archive/.

The raw CSVs are ~123 MB and are deliberately not committed (see .gitignore);
this keeps the repository small and avoids redistributing a CC BY-NC-SA
dataset. Anyone cloning the repo runs `make data` instead.

Requires Kaggle credentials, either as environment variables
(KAGGLE_USERNAME / KAGGLE_KEY) or in ~/.kaggle/kaggle.json.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

DATASET = "olistbr/brazilian-ecommerce"
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "archive"

EXPECTED = {
    "olist_customers_dataset.csv": 99_441,
    "olist_geolocation_dataset.csv": 1_000_163,
    "olist_order_items_dataset.csv": 112_650,
    "olist_order_payments_dataset.csv": 103_886,
    "olist_order_reviews_dataset.csv": 104_719,
    "olist_orders_dataset.csv": 99_441,
    "olist_products_dataset.csv": 32_951,
    "olist_sellers_dataset.csv": 3_095,
    "product_category_name_translation.csv": 70,
}


def verify() -> bool:
    """Check every expected file is present with the expected row count."""
    ok = True
    for name, rows in EXPECTED.items():
        path = RAW / name
        if not path.exists():
            print(f"  MISSING  {name}")
            ok = False
            continue
        with path.open(encoding="utf-8") as handle:
            actual = sum(1 for _ in handle) - 1
        status = "ok" if actual == rows else "ROW COUNT MISMATCH"
        if actual != rows:
            ok = False
        print(f"  {status:<18} {name}  ({actual:,} rows, expected {rows:,})")
    return ok


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)

    if all((RAW / name).exists() for name in EXPECTED):
        print(f"Dataset already present in {RAW}. Verifying...")
        return 0 if verify() else 1

    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
    except ImportError:
        print(
            "kaggle package not installed.\n"
            "  pip install kaggle\n"
            f"Or download {DATASET} manually and unzip it into {RAW}",
            file=sys.stderr,
        )
        return 1

    api = KaggleApi()
    api.authenticate()
    print(f"Downloading {DATASET} -> {RAW}")
    api.dataset_download_files(DATASET, path=str(RAW), quiet=False)

    for archive in RAW.glob("*.zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(RAW)
        archive.unlink()

    print("Verifying...")
    return 0 if verify() else 1


if __name__ == "__main__":
    raise SystemExit(main())
