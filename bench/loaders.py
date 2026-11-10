"""Every load path in the benchmark.

Each loader takes the dataset directory (Parquet + CSV written by
bench.generate) and loads ``raw.raw_customers`` and ``raw.raw_orders`` into
one target. All loaders except ``dbt_seed`` write into tables created with the
same canonical DDL (bench.config.COLUMNS); dbt seed gets the same column types
through ``column_types`` in dbt_project.yml. So dbt reads identical sources no
matter which path loaded them.

Families (used for grouping in BENCH.md):
  seed    - dbt seed from CSV (what many tutorials use to "load data")
  classic - row-oriented DB-API loaders: Python tuples sent with
            cursor.executemany (psycopg3 / duckdb)
  expert  - engine-native bulk paths that skip Python rows (Postgres COPY,
            DuckDB read_csv/read_parquet/Arrow scan). Fast, but engine-specific.
  adbc    - ADBC bulk ingest: Arrow record batches end-to-end through a
            standard, engine-neutral API (adbc_ingest)
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bench.config import COLUMNS, DBT_DIR, RAW_SCHEMA, TABLES, ddl, pg_settings, pg_uri

# ---------------------------------------------------------------- helpers


def _pg_connect():
    import psycopg

    return psycopg.connect(**pg_settings())


def _arrow_csv_reader(path: Path, table: str):
    """Streaming Arrow CSV reader with explicit types (no inference)."""
    import pyarrow as pa
    import pyarrow.csv as pacsv

    arrow_types = {
        "bigint": pa.int64(),
        "text": pa.string(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("us"),
        "boolean": pa.bool_(),
    }
    convert = pacsv.ConvertOptions(
        column_types={name: arrow_types[typ] for name, typ in COLUMNS[table]},
        strings_can_be_null=True,
        quoted_strings_can_be_null=False,
    )
    return pacsv.open_csv(path, convert_options=convert, read_options=pacsv.ReadOptions(block_size=8 << 20))


def _parquet_reader(path: Path):
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    return pf.schema_arrow, pf.iter_batches(batch_size=128_000)


# ---------------------------------------------------------------- dbt seed


def dbt_seed(target: str, data: Path, ctx: dict) -> None:
    """`dbt seed` from CSV. ctx['dbt_runner'] is created before the timer."""
    seeds = DBT_DIR / "seeds"
    for table in TABLES:
        link = seeds / f"{table}.csv"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(data / f"{table}.csv")
    try:
        res = ctx["dbt_runner"].invoke(
            [
                "seed",
                "--project-dir", str(DBT_DIR),
                "--profiles-dir", str(DBT_DIR),
                "--target", target,
                "--full-refresh",
                "--quiet",
            ]
        )
        if not res.success:
            raise RuntimeError(f"dbt seed failed: {res.exception}")
    finally:
        for table in TABLES:
            (seeds / f"{table}.csv").unlink(missing_ok=True)


# ---------------------------------------------------------------- Postgres


def _pg_create_tables() -> None:
    with _pg_connect() as conn:
        for table in TABLES:
            conn.execute(ddl(table))


def pg_psycopg_executemany(data: Path) -> None:
    _pg_create_tables()
    with _pg_connect() as conn, conn.cursor() as cur:
        for table in TABLES:
            cols = [c for c, _ in COLUMNS[table]]
            sql = f"insert into {RAW_SCHEMA}.{table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})"
            _, batches = _parquet_reader(data / f"{table}.parquet")
            for batch in batches:
                columns = [batch.column(c).to_pylist() for c in cols]
                cur.executemany(sql, list(zip(*columns)))


def pg_copy_csv(data: Path) -> None:
    _pg_create_tables()
    with _pg_connect() as conn, conn.cursor() as cur:
        for table in TABLES:
            with cur.copy(f"copy {RAW_SCHEMA}.{table} from stdin with (format csv, header true)") as copy, \
                    open(data / f"{table}.csv", "rb") as f:
                while chunk := f.read(1 << 20):
                    copy.write(chunk)


def _adbc_pg():
    import adbc_driver_postgresql.dbapi as pg

    return pg.connect(pg_uri())


def _adbc_duck(ctx: dict):
    import adbc_driver_duckdb.dbapi as dd

    return dd.connect(str(ctx["duckdb_path"]))


def adbc_ingest(connect: Callable, data: Path, source: str, create: Callable) -> None:
    """source: 'parquet' (whole table in memory), 'parquet_stream' or 'csv_stream'."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    create()
    with connect() as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                if source == "parquet":
                    payload = pq.read_table(data / f"{table}.parquet")
                elif source == "parquet_stream":
                    schema, batches = _parquet_reader(data / f"{table}.parquet")
                    payload = pa.RecordBatchReader.from_batches(schema, batches)
                elif source == "csv_stream":
                    payload = _arrow_csv_reader(data / f"{table}.csv", table)
                else:
                    raise ValueError(source)
                cur.adbc_ingest(table, payload, mode="append", db_schema_name=RAW_SCHEMA)
        conn.commit()


# ---------------------------------------------------------------- DuckDB


def _duck_create_tables(ctx: dict) -> None:
    import duckdb

    with duckdb.connect(str(ctx["duckdb_path"])) as con:
        for table in TABLES:
            con.execute(ddl(table))


def duck_executemany(data: Path, ctx: dict) -> None:
    import duckdb

    _duck_create_tables(ctx)
    with duckdb.connect(str(ctx["duckdb_path"])) as con:
        for table in TABLES:
            cols = [c for c, _ in COLUMNS[table]]
            sql = f"insert into {RAW_SCHEMA}.{table} ({', '.join(cols)}) values ({', '.join(['?'] * len(cols))})"
            _, batches = _parquet_reader(data / f"{table}.parquet")
            for batch in batches:
                columns = [batch.column(c).to_pylist() for c in cols]
                con.executemany(sql, list(zip(*columns)))


def duck_native(data: Path, ctx: dict, kind: str) -> None:
    import duckdb

    _duck_create_tables(ctx)
    with duckdb.connect(str(ctx["duckdb_path"])) as con:
        for table in TABLES:
            if kind == "read_csv":
                types = ", ".join(f"'{c}': '{t}'" for c, t in COLUMNS[table])
                src = f"read_csv('{data / (table + '.csv')}', header = true, columns = {{{types}}})"
                con.execute(f"insert into {RAW_SCHEMA}.{table} select * from {src}")
            elif kind == "read_parquet":
                con.execute(f"insert into {RAW_SCHEMA}.{table} select * from read_parquet('{data / (table + '.parquet')}')")
            elif kind == "arrow_scan":
                import pyarrow.parquet as pq

                arrow_tbl = pq.read_table(data / f"{table}.parquet")  # noqa: F841 (DuckDB replacement scan)
                con.execute(f"insert into {RAW_SCHEMA}.{table} select * from arrow_tbl")
            else:
                raise ValueError(kind)


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class Loader:
    name: str
    target: str  # 'postgres' | 'duckdb'
    family: str  # seed | classic | expert | adbc
    input: str  # csv | parquet
    description: str
    fn: Callable[[Path, dict], None]


def _registry() -> list[Loader]:
    L = Loader
    pg_create = _pg_create_tables
    return [
        # Postgres
        L("dbt_seed", "postgres", "seed", "csv", "dbt seed (agate + batched INSERT)",
          lambda d, c: dbt_seed("postgres", d, c)),
        L("psycopg_executemany", "postgres", "classic", "parquet", "psycopg3 cursor.executemany, 128k-row batches",
          lambda d, c: pg_psycopg_executemany(d)),
        L("psycopg_copy_csv", "postgres", "expert", "csv", "psycopg3 COPY FROM STDIN (CSV), the expert classic baseline",
          lambda d, c: pg_copy_csv(d)),
        L("adbc_parquet", "postgres", "adbc", "parquet", "pq.read_table -> adbc_ingest (adbc-driver-postgresql)",
          lambda d, c: adbc_ingest(_adbc_pg, d, "parquet", pg_create)),
        L("adbc_parquet_stream", "postgres", "adbc", "parquet", "Parquet RecordBatchReader -> adbc_ingest (streaming)",
          lambda d, c: adbc_ingest(_adbc_pg, d, "parquet_stream", pg_create)),
        L("adbc_csv_stream", "postgres", "adbc", "csv", "pyarrow.csv streaming reader -> adbc_ingest",
          lambda d, c: adbc_ingest(_adbc_pg, d, "csv_stream", pg_create)),
        # DuckDB
        L("dbt_seed", "duckdb", "seed", "csv", "dbt seed (dbt-duckdb)",
          lambda d, c: dbt_seed("duckdb", d, c)),
        L("dbapi_executemany", "duckdb", "classic", "parquet", "duckdb DB-API executemany, 128k-row batches",
          lambda d, c: duck_executemany(d, c)),
        L("duckdb_read_csv", "duckdb", "expert", "csv", "INSERT ... SELECT FROM read_csv (DuckDB native)",
          lambda d, c: duck_native(d, c, "read_csv")),
        L("duckdb_read_parquet", "duckdb", "expert", "parquet", "INSERT ... SELECT FROM read_parquet (DuckDB native)",
          lambda d, c: duck_native(d, c, "read_parquet")),
        L("duckdb_arrow_scan", "duckdb", "expert", "parquet", "pq.read_table -> DuckDB Arrow replacement scan",
          lambda d, c: duck_native(d, c, "arrow_scan")),
        L("adbc_parquet", "duckdb", "adbc", "parquet", "pq.read_table -> adbc_ingest (adbc_driver_duckdb)",
          lambda d, c: adbc_ingest(lambda: _adbc_duck(c), d, "parquet", lambda: _duck_create_tables(c))),
        L("adbc_parquet_stream", "duckdb", "adbc", "parquet", "Parquet RecordBatchReader -> adbc_ingest (streaming)",
          lambda d, c: adbc_ingest(lambda: _adbc_duck(c), d, "parquet_stream", lambda: _duck_create_tables(c))),
        L("adbc_csv_stream", "duckdb", "adbc", "csv", "pyarrow.csv streaming reader -> adbc_ingest",
          lambda d, c: adbc_ingest(lambda: _adbc_duck(c), d, "csv_stream", lambda: _duck_create_tables(c))),
    ]


LOADERS: dict[tuple[str, str], Loader] = {(l.target, l.name): l for l in _registry()}


def loaders_for(target: str) -> list[Loader]:
    return [l for l in LOADERS.values() if l.target == target]


def get(target: str, name: str) -> Loader:
    try:
        return LOADERS[(target, name)]
    except KeyError:
        names = ", ".join(l.name for l in loaders_for(target))
        raise SystemExit(f"unknown loader {name!r} for {target}; choose from {names}") from None


os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
