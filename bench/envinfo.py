"""Capture the environment a result was produced on."""

from __future__ import annotations

import os
import platform
import subprocess
from importlib import metadata

PACKAGES = [
    "dbt-core", "dbt-duckdb", "dbt-postgres", "dbt-adapters", "duckdb",
    "pyarrow", "numpy", "psycopg", "psycopg2-binary",
    "adbc-driver-manager", "adbc-driver-postgresql", "agate",
]


def _cpu_model() -> str:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _postgres_version() -> str | None:
    try:
        import psycopg

        from bench.config import pg_settings

        with psycopg.connect(**pg_settings(), connect_timeout=3) as conn:
            version = conn.execute("show server_version").fetchone()[0]
            settings = {
                k: conn.execute(f"show {k}").fetchone()[0]
                for k in ("shared_buffers", "max_wal_size", "synchronous_commit", "fsync")
            }
            return f"{version} {settings}"
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc.__class__.__name__}"


def collect() -> dict:
    import psutil

    versions = {}
    for pkg in PACKAGES:
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            versions[pkg] = None
    try:
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        git = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "ram_bytes": psutil.virtual_memory().total,
        "loadavg_1_5_15": list(os.getloadavg()),
        "postgres": _postgres_version(),
        "packages": versions,
        "git_commit": git or None,
    }
