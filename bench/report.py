"""Render BENCH.md (and a compact latest.json) from harness/verify JSON.

  python -m bench.report results/runs/bench.json --verify results/runs/verify.json \
      --out results/BENCH.md --json results/latest.json

Only measured values are printed. Projections (skipped_projected) are shown
as "skipped" with the projection in parentheses, never as a timing.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from bench.config import SIZES

MiB = 1024 * 1024
FAMILY_ORDER = {"seed": 0, "classic": 1, "expert": 2, "adbc": 3}


def aggregate(doc: dict) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in doc["results"]:
        groups[(r["target"], r["size"], r["loader"])].append(r)
    rows = []
    for (target, size, loader), rs in groups.items():
        meta = doc["loaders"][f"{target}/{loader}"]
        timed = [r for r in rs if r.get("round") != "memory" and r["status"] == "ok"]
        mem = next((r for r in rs if r.get("round") == "memory" and r["status"] == "ok"), None)
        bad = next((r for r in rs if r.get("round") != "memory" and r["status"] != "ok"), None)
        mem_bad = next((r for r in rs if r.get("round") == "memory" and r["status"] != "ok"), None)
        row = {"target": target, "size": size, "loader": loader, **meta}
        if timed:
            walls = sorted(r["wall_s"] for r in timed)
            med = statistics.median(walls)
            n = timed[0]["rows"]
            row.update({
                "status": "ok" if not bad else f"ok ({len(timed)} rounds, then {bad['status']})",
                "rounds": len(walls), "rows": n, "median_s": med, "min_s": walls[0], "max_s": walls[-1],
                "rows_per_s": n / med,
                "peak_rss_mib": statistics.median(r["peak_rss_bytes"] for r in timed) / MiB,
                "peak_rss_delta_mib": statistics.median(r["peak_rss_delta_bytes"] for r in timed) / MiB,
            })
        else:
            row["status"] = bad["status"] if bad else "missing"
            if bad and bad["status"] == "skipped_projected":
                row["projected_s"] = bad["projected_s"]
                row["projected_from"] = bad["projected_from"]
            if bad and bad.get("note"):
                row["note"] = bad["note"]
            if bad and bad["status"] == "error":
                row["error"] = bad.get("stderr_tail", "")[-400:]
        if mem_bad:
            row["memory_round"] = mem_bad["status"]
            row["memory_note"] = mem_bad.get("note")
        if mem:
            row["tracemalloc_peak_mib"] = mem["tracemalloc_peak_bytes"] / MiB
            row["arrow_pool_max_mib"] = mem["arrow_pool_max_bytes"] / MiB
        rows.append(row)
    return rows


def _fmt_s(x: float) -> str:
    return f"{x:.2f}" if x < 100 else f"{x:.0f}"


def _fmt_n(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.2f}M"
    if x >= 1e3:
        return f"{x / 1e3:.0f}k"
    return f"{x:.0f}"


def _mib(x) -> str:
    return "-" if x is None else f"{x:,.0f}"


def table(rows: list[dict], timeout: float) -> list[str]:
    seed = next((r for r in rows if r["loader"] == "dbt_seed" and "median_s" in r), None)
    out = ["| Load path | Family | Input | Median s | Min-max s | Rows/s | vs dbt seed | Peak RSS (+delta) MiB | Py heap peak MiB | Arrow pool MiB |",
           "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    ok = sorted((r for r in rows if "median_s" in r), key=lambda r: r["median_s"])
    rest = sorted((r for r in rows if "median_s" not in r), key=lambda r: FAMILY_ORDER[r["family"]])
    for r in ok:
        ratio = seed["median_s"] / r["median_s"] if seed else None
        speed = "n/a" if ratio is None else (f"{ratio:.1f}x" if ratio >= 1 else f"{ratio:.3f}x")
        out.append(
            f"| `{r['loader']}` | {r['family']} | {r['input']} | **{_fmt_s(r['median_s'])}** | "
            f"{_fmt_s(r['min_s'])}-{_fmt_s(r['max_s'])} | {_fmt_n(r['rows_per_s'])} | {speed} | "
            f"{_mib(r['peak_rss_mib'])} (+{_mib(r['peak_rss_delta_mib'])}) | "
            f"{_mib(r.get('tracemalloc_peak_mib')) if 'memory_round' not in r else 'n/a [1]'} | "
            f"{_mib(r.get('arrow_pool_max_mib')) if 'memory_round' not in r else 'n/a [1]'} |")
    for r in rest:
        if r["status"] == "skipped_projected":
            why = f"skipped: projected {r['projected_s']:.0f} s from {r['projected_from']} > {timeout:.0f} s timeout"
        elif r["status"] == "timeout":
            why = f"timeout (> {timeout:.0f} s)"
        elif r["status"] == "skipped_by_decision":
            why = f"skipped: {r.get('note', 'out of scope')}"
        elif r["status"] == "skipped_after_timeout":
            why = "skipped (timed out at a smaller size)"
        else:
            why = r["status"]
        out.append(f"| `{r['loader']}` | {r['family']} | {r['input']} | {why} | | | | | | |")
    notes = [r for r in rows if r.get("memory_round")]
    for r in notes:
        out += ["", f"[1] `{r['loader']}`: memory round {r['memory_round']}. {r.get('memory_note') or ''}".rstrip()]
    return out


def render(doc: dict, verify: dict | None, seed_mem: dict | None = None) -> tuple[str, dict]:
    rows = aggregate(doc)
    env = doc["env_before"]
    timeout = doc["config"]["timeout"]
    lines = [
        "# Benchmark results",
        "",
        "Generated by `python -m bench.report` from the harness JSON. Every number below was measured "
        "on the machine described in [Environment](#environment); nothing is projected unless it says so.",
        "",
        "* Runs: " + "; ".join(f"{r['started']} -> {r.get('finished') or 'n/a'}"
                               for r in doc.get("runs", [{"started": doc["started"], "finished": doc.get("finished")}])),
        f"* Rounds: {doc['config']['rounds']} timed rounds per cell (median shown) + 1 memory round; "
        f"timeout {timeout:.0f} s per run.",
        "* Rows = orders + customers (customers = orders / 20). Times include reading the input file and "
        "creating the table, exclude Python imports and dataset generation.",
        "* `Peak RSS` = process high-water mark (ru_maxrss); `+delta` = growth during the load. "
        "`Py heap peak` = tracemalloc (Python objects only). `Arrow pool` = pyarrow allocator max. "
        "Memory columns come from separate runs (see README, Methodology).",
        "",
    ]
    for size in sorted({r["size"] for r in rows}, key=SIZES.get):
        for target in ("duckdb", "postgres"):
            sub = [r for r in rows if r["size"] == size and r["target"] == target]
            if not sub:
                continue
            n = next((r["rows"] for r in sub if "rows" in r), None)
            lines += [f"## {target} - {size} orders" + (f" ({n:,} rows total)" if n else ""), ""]
            lines += table(sub, timeout)
            lines.append("")
    if seed_mem:
        lines += ["## dbt seed memory", "",
                  f"Separate run (`python -m bench.seed_memory`), size {seed_mem['size']}, "
                  "median of the runs below. All under tracemalloc, so RSS here includes tracemalloc's own "
                  "bookkeeping and is higher than in the timed tables; wall time is NOT a timing result.", "",
                  "| Target | Runs | Py heap peak MiB | Peak RSS MiB (under tracemalloc) | RSS growth MiB | Wall s (instrumented) |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for t, s_ in seed_mem["summary"].items():
            lines.append(f"| {t} | {s_['runs']} | {s_['tracemalloc_peak_mib_median']:.0f} | {s_['peak_rss_mib_median']:.0f} | "
                         f"{s_['peak_rss_delta_mib_median']:.0f} | {s_['wall_s_under_tracemalloc_median']:.0f} |")
        lines.append("")
    if verify:
        lines += ["## Same dbt build on every load path", "",
                  f"Size {verify['size']}. Each load path loaded the data, then the same "
                  "`dbt build --exclude-resource-type seed` ran on top (5 models, generic + singular tests). "
                  "Fingerprint = row count + sha256 over ordered, normalised rows.", "",
                  "| Target | Load path | dbt build | Build s | raw_orders sha256 | fct_daily_revenue sha256 |",
                  "| --- | --- | --- | ---: | --- | --- |"]
        for run in verify["runs"]:
            b = run.get("dbt_build", {})
            fp = run.get("fingerprints", {})
            st = ", ".join(f"{k} {v}" for k, v in sorted(b.get("statuses", {}).items())) or "-"
            lines.append(
                f"| {run['target']} | `{run['loader']}` | {'pass' if b.get('success') else 'FAIL'} ({st}) | "
                f"{b.get('wall_s', 0):.1f} | `{fp.get('raw.raw_orders', {}).get('sha256', '-')[:12]}` | "
                f"`{fp.get('analytics.fct_daily_revenue', {}).get('sha256', '-')[:12]}` |")
        lines += ["", "| Relation | Identical across load paths | Identical across DuckDB and Postgres |",
                  "| --- | --- | --- |"]
        for rel, s in verify["summary"].items():
            lines.append(f"| `{rel}` | {'yes' if s['identical_across_loaders'] else '**NO**'} | "
                         f"{'yes' if s['identical_across_targets'] else '**NO**'} |")
        lines.append("")
    pk = env["packages"]
    lines += [
        "## Environment", "",
        f"* CPU: {env['cpu']} ({env['cpu_count']} vCPU), RAM {env['ram_bytes'] / 2**30:.1f} GiB, {env['platform']}",
        f"* Load average at start (1/5/15 min): {env['loadavg_1_5_15']}; at end: "
        f"{doc.get('env_after', {}).get('loadavg_1_5_15', 'n/a')}",
        f"* Python {env['python']}; Postgres: {env['postgres']}",
        "* Packages: " + ", ".join(f"{k} {v}" for k, v in pk.items() if v),
        "* Repo commit per run: " + ", ".join(
            str(e.get("git_commit")) for e in [env] + doc.get("env_later_runs", [])),
    ]
    if doc.get("note"):
        lines.append(f"* Note on the first run: {doc['note']}")
    lines.append("")
    compact = {"started": doc["started"], "finished": doc.get("finished"), "config": doc["config"],
               "env": env, "env_after": doc.get("env_after"), "cells": rows,
               "seed_memory": seed_mem["summary"] if seed_mem else None,
               "verify": {"size": verify["size"], "summary": verify["summary"],
                          "all_builds_passed": verify["all_builds_passed"]} if verify else None}
    return "\n".join(lines), compact


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bench", nargs="+", help="one or more harness JSON files (merged in order)")
    ap.add_argument("--verify")
    ap.add_argument("--out", default="results/BENCH.md")
    ap.add_argument("--json")
    ap.add_argument("--seed-memory")
    args = ap.parse_args()
    docs = [json.loads(Path(b).read_text()) for b in args.bench]
    doc = docs[0]
    for extra in docs[1:]:
        cells = {(r["target"], r["size"], r["loader"]) for r in extra["results"]}
        doc["results"] = [r for r in doc["results"] if (r["target"], r["size"], r["loader"]) not in cells]
        doc["results"] += extra["results"]
        doc["loaders"].update(extra["loaders"])
        doc.setdefault("runs", [{"started": doc["started"], "finished": doc.get("finished")}])
        doc["runs"].append({"started": extra["started"], "finished": extra.get("finished")})
        doc["finished"] = extra.get("finished")
        doc["env_after"] = extra.get("env_after")
        doc["env_later_runs"] = doc.get("env_later_runs", []) + [extra["env_before"]]
    verify = json.loads(Path(args.verify).read_text()) if args.verify else None
    seed_mem = json.loads(Path(args.seed_memory).read_text()) if args.seed_memory else None
    md, compact = render(doc, verify, seed_mem)
    Path(args.out).write_text(md)
    if args.json:
        Path(args.json).write_text(json.dumps(compact, indent=2) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
