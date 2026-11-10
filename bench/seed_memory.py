"""Dedicated dbt seed memory measurement (tracemalloc + RSS), N runs per engine.

The tracemalloc round of `dbt seed` at 1m takes far longer than its ~450 s
timed rounds, so seed memory is measured here at a size that finishes.

  python -m bench.seed_memory --size 100k --runs 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path

from bench import envinfo
from bench.config import RESULTS_DIR
from bench.generate import generate
from bench.run import _run_worker

MiB = 1024 * 1024


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", default="100k")
    ap.add_argument("--targets", default="duckdb,postgres")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--out", default=str(RESULTS_DIR / "runs" / "seed-memory.json"))
    args = ap.parse_args()
    generate(args.size)
    doc = {"started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "env": envinfo.collect(), "size": args.size, "results": [], "summary": {}}
    for target in args.targets.split(","):
        runs = []
        for i in range(args.runs):
            res = _run_worker(target, "dbt_seed", args.size, "memory", args.timeout)
            res.update({"target": target, "run": i + 1})
            doc["results"].append(res)
            runs.append(res)
            print(f"[seed memory {args.size} {target} #{i + 1}] {res['status']}", flush=True)
        ok = [r for r in runs if r["status"] == "ok"]
        if ok:
            doc["summary"][target] = {
                "runs": len(ok),
                "tracemalloc_peak_mib_median": statistics.median(r["tracemalloc_peak_bytes"] for r in ok) / MiB,
                "peak_rss_mib_median": statistics.median(r["peak_rss_bytes"] for r in ok) / MiB,
                "peak_rss_delta_mib_median": statistics.median(r["peak_rss_delta_bytes"] for r in ok) / MiB,
                "rss_before_mib_median": statistics.median(r["rss_before_bytes"] for r in ok) / MiB,
                "wall_s_under_tracemalloc_median": statistics.median(r["wall_s"] for r in ok),
            }
    doc["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    Path(args.out).write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps(doc["summary"], indent=2))


if __name__ == "__main__":
    main()
