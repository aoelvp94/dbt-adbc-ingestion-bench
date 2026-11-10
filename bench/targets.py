"""Target lifecycle: reset before a load, count rows, fingerprint tables."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bench.config import ANALYTICS_SCHEMA, RAW_SCHEMA, TABLES, pg_settings


def reset(target: str, duckdb_path: Path | None) -> None:
    """Drop everything a previous run created, then flush Postgres dirty pages
    so one run's checkpoint cost does not land inside the next run's timer."""
    if target == "postgres":
        import psycopg

        with psycopg.connect(**pg_settings(), autocommit=True) as conn:
            conn.execute(f"drop schema if exists {RAW_SCHEMA} cascade")
            conn.execute(f"drop schema if exists {ANALYTICS_SCHEMA} cascade")
            conn.execute(f"create schema {RAW_SCHEMA}")
            conn.execute("checkpoint")
    elif target == "duckdb":
        assert duckdb_path is not None
        for p in (duckdb_path, duckdb_path.with_suffix(".duckdb.wal")):
            p.unlink(missing_ok=True)
        import duckdb

        with duckdb.connect(str(duckdb_path)) as con:
            con.execute(f"create schema {RAW_SCHEMA}")
    else:
        raise ValueError(target)


def _query(target: str, duckdb_path: Path | None, sql: str):
    """Run a query and return an Arrow table (ADBC on both engines)."""
    if target == "postgres":
        import adbc_driver_postgresql.dbapi as pg

        from bench.config import pg_uri

        conn = pg.connect(pg_uri())
    else:
        import adbc_driver_duckdb.dbapi as dd

        conn = dd.connect(str(duckdb_path))
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetch_arrow_table()
    finally:
        conn.close()


def row_counts(target: str, duckdb_path: Path | None) -> dict[str, int]:
    sql = " union all ".join(f"select '{t}' as t, count(*) as n from {RAW_SCHEMA}.{t}" for t in TABLES)
    tbl = _query(target, duckdb_path, sql).to_pylist()
    return {r["t"]: int(r["n"]) for r in tbl}


def _normalise(value):
    """Engine-neutral text form so fingerprints compare across Postgres/DuckDB."""
    import datetime as dt
    import decimal

    if value is None:
        return None
    if isinstance(value, decimal.Decimal):
        return str(value.normalize()) if value != value.to_integral_value() else str(int(value))
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, (dt.date, bool, int, str)):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


def fingerprint(target: str, duckdb_path: Path | None, relation: str, order_by: str) -> dict:
    """Row count + sha256 over every row (ordered, normalised).

    Pulls the whole relation through Arrow, so use it on raw tables only up to
    a few million rows (the verify step defaults to 100k)."""
    tbl = _query(target, duckdb_path, f"select * from {relation} order by {order_by}")
    h = hashlib.sha256()
    cols = tbl.column_names
    h.update(json.dumps(cols).encode())
    for batch in tbl.to_batches(max_chunksize=200_000):
        data = [batch.column(c).to_pylist() for c in cols]
        for row in zip(*data):
            h.update(json.dumps([_normalise(v) for v in row]).encode())
            h.update(b"\n")
    return {"rows": tbl.num_rows, "sha256": h.hexdigest()}
