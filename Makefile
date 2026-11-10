# Thin wrapper for people without `just`. Same recipes as the justfile.
.PHONY: setup data test quick bench verify report reproduce clean
PY := uv run --frozen python

setup: ; uv sync --frozen && docker compose up -d --wait
data: ; for s in 100k 1m 10m; do $(PY) -m bench.generate --size $$s > /dev/null; done
test: ; uv run --frozen pytest -q
quick:
	$(PY) -m bench.run --sizes 100k --rounds 1 --no-memory --out results/runs/quick.json
	$(PY) -m bench.report results/runs/quick.json --out results/QUICK.md
bench: ; $(PY) -m bench.run --sizes 100k,1m,10m --rounds 3 --timeout 600 --out results/runs/bench.json
verify: ; $(PY) -m bench.verify --size 100k --out results/runs/verify.json
report: ; $(PY) -m bench.report results/runs/bench.json --verify results/runs/verify.json --out results/BENCH.md --json results/latest.json
reproduce: setup data test bench verify report
clean: ; docker compose down -v; rm -rf data work results/runs
