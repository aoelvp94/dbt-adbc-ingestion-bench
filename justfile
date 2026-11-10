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
    {{py}} -m bench.run --sizes {{sizes}} --rounds {{rounds}} --timeout {{timeout}} --out results/runs/bench.json

# Same dbt build on top of every load path; fingerprints must match
verify size="100k":
    {{py}} -m bench.verify --size {{size}} --out results/runs/verify.json

# Render results/BENCH.md + results/latest.json from the last bench + verify
report:
    {{py}} -m bench.report results/runs/bench.json --verify results/runs/verify.json --out results/BENCH.md --json results/latest.json

# Everything a reviewer needs, end to end
reproduce: setup data test bench verify report

# Stop Postgres and delete generated data and DuckDB files
clean:
    docker compose down -v
    rm -rf data work results/runs
