"""Benchmark harness: every loader x target x size, N rounds, fresh process each.

  python -m bench.run --sizes 100k,1m --targets duckdb,postgres --rounds 3

Methodology (also in README):
* Each measurement is a fresh `python -m bench.worker` process. The target is
  reset (schema dropped, Postgres CHECKPOINT, DuckDB file deleted) before it.
* Rounds are interleaved: round 1 runs every loader once in a shuffled
  (seeded) order, then round 2, ... so slow drift on the machine hits every
  loader roughly equally instead of one loader's three rounds in a row.
* Timing rounds run without instrumentation. One extra `memory` round per
  loader runs under tracemalloc + Arrow pool tracking; its wall time is
  discarded.
* Timeout: a run over --timeout seconds is killed and recorded as a timeout.
  Before a larger size, the previous size's median is scaled linearly; if the
  projection exceeds the timeout the loader is skipped and the result is
  labelled `skipped_projected` (a projection, never reported as a measurement).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

from bench import envinfo, loaders
from bench.config import RESULTS_DIR, SIZES, size_label
from bench.generate import generate


def _run_worker(target: str, loader: str, size: str, mode: str, timeout: float) -> dict:
    cmd = [sys.executable, "-m", "bench.worker", "--target", target, "--loader", loader,
           "--size", size, "--mode", mode, "--tag", f"bench-{target}-{loader}"]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "elapsed_s": time.perf_counter() - t0}
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            res = json.loads(line[len("RESULT "):])
            res["status"] = "ok"
            return res
    return {"status": "error", "returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="100k,1m,10m")
    ap.add_argument("--targets", default="duckdb,postgres")
    ap.add_argument("--loaders", default="", help="comma list to restrict (default: all)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--no-memory", action="store_true", help="skip the tracemalloc round")
    ap.add_argument("--out", default="")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--skip", action="append", default=[], metavar="LOADER@SIZE",
                    help="do not run LOADER at SIZE on any target (recorded as skipped_by_decision), "
                         "e.g. --skip dbt_seed@10m")
    ap.add_argument("--prior", action="append", default=[],
                    help="earlier run JSON whose medians seed the timeout projection (resume a run)")
    args = ap.parse_args()

    sizes = sorted((size_label(s) for s in args.sizes.split(",")), key=SIZES.get)
    tgts = [t for t in args.targets.split(",") if t]
    only = {x for x in args.loaders.split(",") if x}
    rng = random.Random(args.seed)
    started = dt.datetime.now().astimezone()
    out = Path(args.out) if args.out else RESULTS_DIR / "runs" / f"{started:%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = {
        "started": started.isoformat(timespec="seconds"),
        "config": vars(args),
        "env_before": envinfo.collect(),
        "loaders": {f"{l.target}/{l.name}": {"family": l.family, "input": l.input, "description": l.description}
                    for l in loaders.LOADERS.values()},
        "results": [],
    }

    def save() -> None:
        out.write_text(json.dumps(doc, indent=2) + "\n")

    last_median: dict[tuple[str, str], tuple[str, float]] = {}
    for prior in args.prior:
        walls_by: dict[tuple[str, str, str], list[float]] = {}
        for r in json.loads(Path(prior).read_text())["results"]:
            if r["status"] == "ok" and r.get("round") != "memory":
                walls_by.setdefault((r["target"], r["loader"], r["size"]), []).append(r["wall_s"])
        for (t, name, size), w in sorted(walls_by.items(), key=lambda kv: SIZES[kv[0][2]]):
            last_median[(t, name)] = (size, statistics.median(w))
    doc["config"]["prior_medians"] = {f"{t}/{n}": v for (t, n), v in last_median.items()}
    dead: set[tuple[str, str]] = set()
    for size in sizes:
        generate(size)
        for target in tgts:
            names = [l.name for l in loaders.loaders_for(target) if not only or l.name in only]
            active = []
            for name in names:
                key = (target, name)
                if f"{name}@{size}" in args.skip:
                    doc["results"].append({"target": target, "loader": name, "size": size,
                                           "status": "skipped_by_decision",
                                           "note": f"excluded with --skip {name}@{size} to bound total runtime"})
                    print(f"[{size} {target}] skip {name}: --skip", flush=True)
                    continue
                if key in dead:
                    doc["results"].append({"target": target, "loader": name, "size": size, "status": "skipped_after_timeout"})
                    continue
                if key in last_median:
                    prev_size, med = last_median[key]
                    projected = med * SIZES[size] / SIZES[prev_size]
                    if projected > args.timeout:
                        doc["results"].append({"target": target, "loader": name, "size": size,
                                               "status": "skipped_projected", "projected_s": projected,
                                               "projected_from": prev_size})
                        print(f"[{size} {target}] skip {name}: projected {projected:.0f}s > timeout", flush=True)
                        continue
                active.append(name)
            walls: dict[str, list[float]] = {n: [] for n in active}
            for rnd in range(1, args.rounds + 1):
                order = active[:]
                rng.shuffle(order)
                for name in order:
                    if (target, name) in dead:
                        continue
                    res = _run_worker(target, name, size, "time", args.timeout)
                    res.update({"target": target, "loader": name, "size": size, "round": rnd})
                    doc["results"].append(res)
                    save()
                    msg = f"{res['wall_s']:.2f}s" if res["status"] == "ok" else res["status"]
                    print(f"[{size} {target} r{rnd}] {name}: {msg}", flush=True)
                    if res["status"] == "ok":
                        walls[name].append(res["wall_s"])
                    else:
                        dead.add((target, name))
            for name, w in walls.items():
                if w and (target, name) not in dead:
                    last_median[(target, name)] = (size, statistics.median(w))
            if not args.no_memory:
                for name in active:
                    if (target, name) in dead:
                        continue
                    res = _run_worker(target, name, size, "memory", args.timeout * 3)
                    res.update({"target": target, "loader": name, "size": size, "round": "memory"})
                    doc["results"].append(res)
                    save()
                    print(f"[{size} {target} mem] {name}: {res['status']}", flush=True)

    doc["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    doc["env_after"] = {"loadavg_1_5_15": envinfo.collect()["loadavg_1_5_15"]}
    save()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
