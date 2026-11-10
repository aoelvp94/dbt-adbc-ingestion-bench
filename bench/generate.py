"""Deterministic synthetic dataset: customers + orders, written as Parquet and CSV.

Same seed + same size => byte-identical Parquet/CSV (checked via sha256 in
manifest.json). Vectorised with numpy + pyarrow, so 10M orders take seconds.

Usage: python -m bench.generate --size 1m
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from bench.config import DATA_DIR, SIZES, customers_for, size_label

SEED = 20261110
COUNTRIES = ["AR", "BR", "CL", "CO", "MX", "PE", "UY", "ES", "PT", "FR", "DE", "US"]
STATUSES = ["placed", "shipped", "delivered", "returned", "cancelled"]
STATUS_P = [0.10, 0.15, 0.65, 0.06, 0.04]
COUPONS = ["WELCOME10", "SPRING5", "VIP20"]
# Parquet row groups of 128k rows let streaming readers keep memory flat.
ROW_GROUP = 128_000


def _prefixed(prefix: str, ids: pa.Array, suffix: str = "") -> pa.Array:
    s = pc.cast(ids, pa.string())
    return pc.binary_join_element_wise(prefix, s, suffix, "")


def customers(n: int, rng: np.random.Generator) -> pa.Table:
    ids = pa.array(np.arange(1, n + 1, dtype=np.int64))
    signup_days = rng.integers(0, 5 * 365, size=n)  # 2020-01-01 .. ~2024-12-30
    signup = pa.array(np.datetime64("2020-01-01") + signup_days.astype("timedelta64[D]"))
    return pa.table(
        {
            "customer_id": ids,
            "full_name": _prefixed("Customer ", ids),
            "email": _prefixed("customer", ids, "@example.com"),
            "country": pa.array(COUNTRIES).take(pa.array(rng.integers(0, len(COUNTRIES), size=n))),
            "signup_date": pc.cast(signup, pa.date32()),
        }
    )


def orders(n: int, n_customers: int, rng: np.random.Generator) -> pa.Table:
    secs = rng.integers(0, 366 * 86_400, size=n)  # calendar year 2024
    ts = (np.datetime64("2024-01-01T00:00:00", "s") + secs.astype("timedelta64[s]")).astype("datetime64[us]")
    has_coupon = rng.random(n) < 0.2
    coupon_idx = rng.integers(0, len(COUPONS), size=n)
    coupon = pa.array(COUPONS).take(pa.array(coupon_idx, mask=~has_coupon))
    return pa.table(
        {
            "order_id": pa.array(np.arange(1, n + 1, dtype=np.int64)),
            "customer_id": pa.array(rng.integers(1, n_customers + 1, size=n, dtype=np.int64)),
            "order_ts": pa.array(ts, type=pa.timestamp("us")),
            "status": pa.array(STATUSES).take(pa.array(rng.choice(len(STATUSES), size=n, p=STATUS_P))),
            "amount_cents": pa.array(rng.integers(100, 50_000, size=n, dtype=np.int64)),
            "is_gift": pa.array(rng.random(n) < 0.1),
            "coupon_code": coupon,
        }
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset_dir(label: str) -> Path:
    return DATA_DIR / label


def generate(label: str, force: bool = False) -> Path:
    label = size_label(label)
    out = dataset_dir(label)
    manifest = out / "manifest.json"
    if manifest.exists() and not force:
        return out
    out.mkdir(parents=True, exist_ok=True)
    n_orders = SIZES[label]
    n_customers = customers_for(n_orders)
    rng = np.random.default_rng(SEED)
    tables = {"raw_customers": customers(n_customers, rng), "raw_orders": orders(n_orders, n_customers, rng)}
    info = {"size": label, "seed": SEED, "tables": {}}
    for name, table in tables.items():
        pq_path = out / f"{name}.parquet"
        csv_path = out / f"{name}.csv"
        pq.write_table(table, pq_path, row_group_size=ROW_GROUP, compression="snappy")
        pacsv.write_csv(table, csv_path)
        info["tables"][name] = {
            "rows": table.num_rows,
            "parquet_bytes": pq_path.stat().st_size,
            "csv_bytes": csv_path.stat().st_size,
            "parquet_sha256": _sha256(pq_path),
            "csv_sha256": _sha256(csv_path),
        }
    manifest.write_text(json.dumps(info, indent=2) + "\n")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", action="append", required=True, help="1k, 10k, 100k, 1m, 10m (repeatable)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    for label in args.size:
        out = generate(label, force=args.force)
        print(json.dumps(json.loads((out / "manifest.json").read_text()), indent=2))


if __name__ == "__main__":
    main()
