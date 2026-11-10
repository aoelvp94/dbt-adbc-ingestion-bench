"""Prove the load path does not change dbt's output.

For every loader on every target: load the dataset, run the SAME
`dbt build` (staging + marts + data tests, seeds excluded), then fingerprint
the raw tables and every mart (row count + sha256 over ordered, normalised
rows). All fingerprints must match across load paths, and the mart
fingerprints must also match across DuckDB and Postgres.

  python -m bench.verify --size 100k
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bench import envinfo, loaders, targets
from bench.config import ANALYTICS_SCHEMA, DBT_DIR, RAW_SCHEMA, RESULTS_DIR, duckdb_path, size_label
from bench.generate import generate

RELATIONS = {
    f"{RAW_SCHEMA}.raw_customers": "customer_id",
    f"{RAW_SCHEMA}.raw_orders": "order_id",
    f"{ANALYTICS_SCHEMA}.dim_customers": "customer_id",
    f"{ANALYTICS_SCHEMA}.fct_daily_revenue": "order_date",
    f"{ANALYTICS_SCHEMA}.fct_country_status": "country_code, status",
}


def dbt_build(target: str, dpath: Path | None) -> dict:
    env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="false")
    if dpath:
        env["DBT_DUCKDB_PATH"] = str(dpath)
    dbt = Path(sys.executable).parent / "dbt"
    cmd = [str(dbt), "build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
           "--target", target, "--exclude-resource-type", "seed"]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    wall = time.perf_counter() - t0
    statuses: dict[str, int] = {}
    rr = DBT_DIR / "target" / "run_results.json"
    if rr.exists():
        for r in json.loads(rr.read_text())["results"]:
            kind = r["unique_id"].split(".")[0]
            key = f"{kind}:{r['status']}"
            statuses[key] = statuses.get(key, 0) + 1
    return {"success": proc.returncode == 0, "wall_s": wall, "statuses": statuses,
            "log_tail": None if proc.returncode == 0 else proc.stdout[-3000:]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", default="100k")
    ap.add_argument("--targets", default="duckdb,postgres")
    ap.add_argument("--exclude", default="", help="comma list of loaders to skip (e.g. slow ones)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    size = size_label(args.size)
    generate(size)
    exclude = {x for x in args.exclude.split(",") if x}
    started = dt.datetime.now().astimezone()
    out = Path(args.out) if args.out else RESULTS_DIR / "runs" / f"verify-{size}-{started:%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"started": started.isoformat(timespec="seconds"), "size": size, "env": envinfo.collect(), "runs": []}

    for target in args.targets.split(","):
        for loader in loaders.loaders_for(target):
            if loader.name in exclude:
                continue
            tag = f"verify-{target}-{loader.name}"
            dpath = duckdb_path(tag) if target == "duckdb" else None
            cmd = [sys.executable, "-m", "bench.worker", "--target", target, "--loader", loader.name,
                   "--size", size, "--tag", tag]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                doc["runs"].append({"target": target, "loader": loader.name, "load_ok": False,
                                    "stderr_tail": proc.stderr[-2000:]})
                continue
            build = dbt_build(target, dpath)
            fps = {}
            if build["success"]:
                for rel, order in RELATIONS.items():
                    fps[rel] = targets.fingerprint(target, dpath, rel, order)
            doc["runs"].append({"target": target, "loader": loader.name, "load_ok": True,
                                "dbt_build": build, "fingerprints": fps})
            print(f"[verify {size} {target}] {loader.name}: dbt build "
                  f"{'ok' if build['success'] else 'FAILED'} {build['statuses']}", flush=True)
            out.write_text(json.dumps(doc, indent=2) + "\n")

    # Cross-check: every relation must have exactly one fingerprint per target,
    # and marts must agree across targets.
    summary: dict[str, dict] = {}
    for rel in RELATIONS:
        per_target: dict[str, set[str]] = {}
        for run in doc["runs"]:
            fp = run.get("fingerprints", {}).get(rel)
            if fp:
                per_target.setdefault(run["target"], set()).add(fp["sha256"])
        all_hashes = set().union(*per_target.values()) if per_target else set()
        summary[rel] = {
            "distinct_hashes_per_target": {t: len(h) for t, h in per_target.items()},
            "identical_across_loaders": all(len(h) == 1 for h in per_target.values()),
            "identical_across_targets": len(all_hashes) == 1,
        }
    doc["summary"] = summary
    doc["all_builds_passed"] = all(r.get("dbt_build", {}).get("success") for r in doc["runs"])
    doc["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    out.write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"all dbt builds passed: {doc['all_builds_passed']}\nwrote {out}")
    if not doc["all_builds_passed"] or not all(s["identical_across_loaders"] for s in summary.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
