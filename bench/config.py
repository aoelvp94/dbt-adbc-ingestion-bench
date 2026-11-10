"""Shared configuration: paths, sizes, connection settings, canonical DDL."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORK_DIR = ROOT / "work"
RESULTS_DIR = ROOT / "results"
DBT_DIR = ROOT / "dbt_project"

RAW_SCHEMA = "raw"
ANALYTICS_SCHEMA = "analytics"
TABLES = ("raw_customers", "raw_orders")

# Size label -> number of order rows. Customers = orders // 20.
SIZES = {
    "1k": 1_000,
    "10k": 10_000,
    "100k": 100_000,
    "1m": 1_000_000,
    "10m": 10_000_000,
}


def customers_for(orders: int) -> int:
    return max(50, orders // 20)


def size_label(label: str) -> str:
    label = label.lower()
    if label not in SIZES:
        raise SystemExit(f"unknown size {label!r}; choose from {', '.join(SIZES)}")
    return label


def pg_settings() -> dict[str, str]:
    return {
        "host": os.environ.get("PG_HOST", "127.0.0.1"),
        "port": os.environ.get("PG_PORT", "55432"),
        "user": os.environ.get("PG_USER", "bench"),
        "password": os.environ.get("PG_PASSWORD", "bench"),
        "dbname": os.environ.get("PG_DB", "bench"),
    }


def pg_uri(driver: str = "postgresql") -> str:
    s = pg_settings()
    return f"{driver}://{s['user']}:{s['password']}@{s['host']}:{s['port']}/{s['dbname']}"


def duckdb_path(tag: str) -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return WORK_DIR / f"{tag}.duckdb"


# Canonical column order and types. Every Python loader writes into a table
# created with this DDL, and dbt seed gets the same types via column_types in
# dbt_project.yml, so dbt sees identical sources regardless of the load path.
COLUMNS = {
    "raw_customers": [
        ("customer_id", "bigint"),
        ("full_name", "text"),
        ("email", "text"),
        ("country", "text"),
        ("signup_date", "date"),
    ],
    "raw_orders": [
        ("order_id", "bigint"),
        ("customer_id", "bigint"),
        ("order_ts", "timestamp"),
        ("status", "text"),
        ("amount_cents", "bigint"),
        ("is_gift", "boolean"),
        ("coupon_code", "text"),
    ],
}


def ddl(table: str, schema: str = RAW_SCHEMA) -> str:
    cols = ", ".join(f"{name} {typ}" for name, typ in COLUMNS[table])
    return f"create table {schema}.{table} ({cols})"
