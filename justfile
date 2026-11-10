# dbt + ADBC ingestion bench. `just` lists recipes.
# No `just`? Every recipe is one or two plain commands; copy them from here.

set dotenv-load := true

py := "uv run --frozen python"

default:
    @just --list

# Install the pinned Python stack and start Postgres in Docker
setup:
    uv sync --frozen
    docker compose up -d --wait

# Generate the deterministic datasets (Parquet + CSV) under data/
data sizes="100k 1m 10m":
    for s in {{sizes}}; do {{py}} -m bench.generate --size $s > /dev/null; done
    @cat data/*/manifest.json | grep -E '"size"|"rows"'

# Unit + smoke tests (DuckDB only, ~1 min, no Docker needed)
test:
    uv run --frozen pytest -q

# Quick look: 100k rows, 1 round, both targets (~5 min)
quick:
    {{py}} -m bench.run --sizes 100k --rounds 1 --no-memory --out results/runs/quick.json
    {{py}} -m bench.report results/runs/quick.json --out results/QUICK.md

# DuckDB only, no Docker at all
bench-duckdb sizes="100k,1m" rounds="3":
    {{py}} -m bench.run --targets duckdb --sizes {{sizes}} --rounds {{rounds}}

# Full benchmark (both targets, 100k/1m/10m, 3 rounds). Takes 1-2 hours.
bench sizes="100k,1m,10m" rounds="3" timeout="600":
    {{py}} -m bench.run --sizes {{sizes}} --rounds {{rounds}} --timeout {{timeout}} --skip dbt_seed@10m --out results/runs/bench.json

# Same dbt build on top of every load path; fingerprints must match
verify size="100k":
    {{py}} -m bench.verify --size {{size}} --out results/runs/verify.json

# dbt seed memory under tracemalloc (its 1m tracemalloc round is too slow for the main run)
seed-memory size="100k" runs="3":
    {{py}} -m bench.seed_memory --size {{size}} --runs {{runs}} --out results/runs/seed-memory.json

# Render results/BENCH.md + results/latest.json from your runs
report:
    {{py}} -m bench.report results/runs/bench.json --verify results/runs/verify.json --seed-memory results/runs/seed-memory.json --out results/BENCH.md --json results/latest.json

# Re-render the committed snapshot (no database needed)
report-snapshot:
    {{py}} -m bench.report results/snapshot-2026-11-10/bench-100k.json results/snapshot-2026-11-10/bench-1m-10m.json --verify results/snapshot-2026-11-10/verify-100k.json --seed-memory results/snapshot-2026-11-10/seed-memory-100k.json --out results/BENCH.md --json results/latest.json

# Everything a reviewer needs, end to end
reproduce: setup data test bench seed-memory verify report

# Stop Postgres and delete generated data and DuckDB files
clean:
    docker compose down -v
    rm -rf data work results/runs
