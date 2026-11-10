"""Every DuckDB load path must produce byte-identical raw tables (no Docker)."""

import pytest

from bench import loaders, targets
from bench.config import TABLES, duckdb_path
from bench.generate import generate

FAST = ["duckdb_read_csv", "duckdb_read_parquet", "duckdb_arrow_scan",
        "adbc_parquet", "adbc_parquet_stream", "adbc_csv_stream", "dbt_seed"]


@pytest.fixture(scope="module")
def data():
    return generate("1k")


def _load(name, data):
    path = duckdb_path(f"pytest-{name}")
    targets.reset("duckdb", path)
    ctx = {"duckdb_path": path}
    if name == "dbt_seed":
        import os

        from dbt.cli.main import dbtRunner

        os.environ["DBT_DUCKDB_PATH"] = str(path)
        ctx["dbt_runner"] = dbtRunner()
    loaders.get("duckdb", name).fn(data, ctx)
    return path


def test_all_duckdb_paths_match(data):
    ref = None
    for name in FAST:
        path = _load(name, data)
        fps = {t: targets.fingerprint("duckdb", path, f"raw.{t}", t.removeprefix("raw_")[:-1] + "_id") for t in TABLES}
        assert fps["raw_orders"]["rows"] == 1000
        if ref is None:
            ref = fps
        assert fps == ref, f"{name} differs from {FAST[0]}"
