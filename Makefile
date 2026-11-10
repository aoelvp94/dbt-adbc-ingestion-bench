# Thin wrapper for people without `just`. Same recipes as the justfile.
.PHONY: setup data test quick bench seed-memory verify report report-snapshot reproduce clean
PY := uv run --frozen python

setup: ; uv sync --frozen && docker compose up -d --wait
data: ; for s in 100k 1m 10m; do $(PY) -m bench.generate --size $$s > /dev/null; done
test: ; uv run --frozen pytest -q
quick:
	$(PY) -m bench.run --sizes 100k --rounds 1 --no-memory --out results/runs/quick.json
	$(PY) -m bench.report results/runs/quick.json --out results/QUICK.md
bench: ; $(PY) -m bench.run --sizes 100k,1m,10m --rounds 3 --timeout 600 --skip dbt_seed@10m --out results/runs/bench.json
verify: ; $(PY) -m bench.verify --size 100k --out results/runs/verify.json
seed-memory: ; $(PY) -m bench.seed_memory --size 100k --runs 3 --out results/runs/seed-memory.json
report: ; $(PY) -m bench.report results/runs/bench.json --verify results/runs/verify.json --seed-memory results/runs/seed-memory.json --out results/BENCH.md --json results/latest.json
SNAP := results/snapshot-2026-11-10
report-snapshot: ; $(PY) -m bench.report $(SNAP)/bench-100k.json $(SNAP)/bench-1m-10m.json --verify $(SNAP)/verify-100k.json --seed-memory $(SNAP)/seed-memory-100k.json --out results/BENCH.md --json results/latest.json
reproduce: setup data test bench seed-memory verify report
clean: ; docker compose down -v; rm -rf data work results/runs
