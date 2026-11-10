"""Run ONE load in a fresh process and print one JSON result line.

A fresh process per measurement means no warm imports, caches, or leaked
memory from a previous loader. Imports and connection objects that are not
part of the load itself are created *before* the timer starts.

Modes:
  time    wall clock + peak RSS (cheap, no instrumentation overhead)
  memory  same load under tracemalloc + Arrow memory-pool tracking. Slower,
          so its wall time is never reported as a timing result.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import tracemalloc

from bench import loaders, targets
from bench.config import duckdb_path
from bench.generate import dataset_dir


def _rss_bytes() -> int:
    import psutil

    return psutil.Process().memory_info().rss


def run(target: str, loader_name: str, size: str, mode: str, tag: str) -> dict:
    loader = loaders.get(target, loader_name)
    data = dataset_dir(size)
    if not (data / "manifest.json").exists():
        raise SystemExit(f"dataset {size} missing; run: python -m bench.generate --size {size}")
    dpath = duckdb_path(tag) if target == "duckdb" else None
    targets.reset(target, dpath)

    # Warm the imports every loader needs so import time is not measured.
    import adbc_driver_duckdb.dbapi  # noqa: F401
    import adbc_driver_postgresql.dbapi  # noqa: F401
    import duckdb  # noqa: F401
    import psycopg  # noqa: F401
    import pyarrow as pa
    import pyarrow.csv  # noqa: F401
    import pyarrow.parquet  # noqa: F401

    ctx: dict = {"duckdb_path": dpath}
    if loader.family == "seed":
        import os

        os.environ["DBT_DUCKDB_PATH"] = str(dpath) if dpath else ""
        from dbt.cli.main import dbtRunner

        ctx["dbt_runner"] = dbtRunner()

    rss_before = _rss_bytes()
    maxrss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    pool = pa.default_memory_pool()
    if mode == "memory":
        tracemalloc.start()

    t0 = time.perf_counter()
    loader.fn(data, ctx)
    wall = time.perf_counter() - t0

    out: dict = {}
    if mode == "memory":
        _, py_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        out["tracemalloc_peak_bytes"] = py_peak
        out["arrow_pool_max_bytes"] = pool.max_memory()
        out["arrow_pool_backend"] = pool.backend_name
    maxrss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    counts = targets.row_counts(target, dpath)
    rows = sum(counts.values())
    out.update(
        {
            "target": target,
            "loader": loader.name,
            "family": loader.family,
            "input": loader.input,
            "size": size,
            "mode": mode,
            "wall_s": wall,
            "rows": rows,
            "row_counts": counts,
            "rows_per_s": rows / wall if wall else None,
            "rss_before_bytes": rss_before,
            "peak_rss_bytes": maxrss_after,
            # Growth of the high-water mark during the load. If imports had
            # already pushed the peak higher, this is a lower bound.
            "peak_rss_delta_bytes": max(0, maxrss_after - max(maxrss_before, rss_before)),
        }
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, choices=["duckdb", "postgres"])
    ap.add_argument("--loader", required=True)
    ap.add_argument("--size", required=True)
    ap.add_argument("--mode", default="time", choices=["time", "memory"])
    ap.add_argument("--tag", default="bench", help="DuckDB file name under work/")
    args = ap.parse_args()
    result = run(args.target, args.loader, args.size, args.mode, args.tag)
    sys.stdout.write("RESULT " + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
