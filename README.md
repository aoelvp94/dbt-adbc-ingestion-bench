# dbt + ADBC ingestion bench

**How should raw data get into the warehouse before `dbt build`?**
This repo loads the same synthetic shop dataset (customers + orders) into
**DuckDB** and **Postgres** through every common load path, measures each
one, then runs the **same dbt project** on top of every load and proves the
models come out identical.

Load paths compared:

| Family | Load path | What it does |
| --- | --- | --- |
| seed | `dbt_seed` | `dbt seed` from CSV, what many tutorials use to "load data" |
| classic | `psycopg_executemany` (Postgres), `dbapi_executemany` (DuckDB) | Python tuples sent with DB-API `cursor.executemany` |
| expert | `psycopg_copy_csv` (Postgres), `duckdb_read_csv` / `duckdb_read_parquet` / `duckdb_arrow_scan` (DuckDB) | The engine's own bulk path. Fast, but different code for every engine |
| adbc | `adbc_parquet`, `adbc_parquet_stream`, `adbc_csv_stream` | [ADBC](https://arrow.apache.org/adbc/) bulk ingest: Arrow record batches end-to-end, one API for every engine |

Everything runs locally: DuckDB needs nothing, Postgres runs in Docker.
No cloud account, no credentials. Snowflake is [documented](docs/snowflake.md)
but never run.

> Results: [results/BENCH.md](results/BENCH.md) (rendered tables),
> [results/latest.json](results/latest.json) (every cell + environment + versions) and the raw
> per-round JSON in [results/snapshot-2026-11-10/](results/snapshot-2026-11-10/).
> `just report-snapshot` re-renders BENCH.md from those files without any database.

## TL;DR

Measured on a shared 8 vCPU VM, 1,050,000 rows (1m orders + 52.5k customers),
median of 3 fresh-process rounds. Full tables for 100k / 1m / 10m in
[results/BENCH.md](results/BENCH.md).

| Load path | DuckDB s | Postgres s | Same code on both engines? |
| --- | ---: | ---: | --- |
| `dbt seed` (CSV) | 7.99 | **449** | yes, but see below |
| DB-API `executemany` | skipped (projected ~1250 s from 100k) | 11.15 | yes |
| Engine-native: `COPY` CSV (Postgres), `read_parquet` (DuckDB) | 0.29 | 0.95 | no |
| ADBC `adbc_ingest`, streaming Parquet | 0.57 | **0.62** | **yes** |

* `dbt seed` into Postgres ran at ~2.3k rows/s at both 100k and 1m: 449 s for
  1m rows, **~720x slower** than ADBC streaming ingest (0.62 s).
* The same `dbt build` (5 models, 20 tests) passed on top of all 14 load paths,
  and every raw table and mart had the **same sha256** across load paths and
  across DuckDB and Postgres. The load path changes how long you wait, not
  what dbt produces.
* ADBC is not the fastest on every engine: DuckDB's own `read_parquet` is
  up to ~3x faster than ADBC ingest into DuckDB (2x at 1m, 2.9x at 10m). ADBC's
  value is one engine-neutral API that stays within that range of the native
  path on both engines and is 14x to ~900x faster than `dbt seed` and
  `executemany`.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and, for the Postgres half, Docker.
[`just`](https://github.com/casey/just) is optional; there is a `Makefile`
with the same recipes, and each recipe is one or two plain commands.

```bash
git clone <this repo> && cd dbt-adbc-ingestion-bench
just setup        # uv sync (pinned versions) + Postgres 18 in Docker on port 55432
just data         # generate 100k / 1m / 10m datasets under data/ (~1 GB, ~10 s)
just test         # unit + smoke tests, DuckDB only (~10 s)
just quick        # 100k rows, 1 round, both engines (~5 min) -> results/QUICK.md
```

DuckDB only, no Docker at all:

```bash
uv sync
uv run python -m bench.generate --size 100k
uv run python -m bench.run --targets duckdb --sizes 100k --rounds 1
```

Full reproduction (what produced `results/`), 1-2 hours mostly spent waiting on `dbt seed`:

```bash
just reproduce    # setup, data, test, bench, seed-memory, verify, report
```

If your Docker daemon is not on the default socket (for example a TCP
daemon), export `DOCKER_HOST` first, e.g. `export DOCKER_HOST=tcp://127.0.0.1:2375`.

## What is in the repo

```
bench/generate.py   deterministic dataset generator (numpy + pyarrow, fixed seed, sha256 manifest)
bench/loaders.py    every load path; read this file first, each loader is ~10 lines
bench/worker.py     runs ONE load in a fresh process, prints a JSON result
bench/run.py        harness: loaders x engines x sizes x rounds, timeouts, memory round
bench/verify.py     loads with every path, runs the same `dbt build`, fingerprints tables
bench/report.py     renders results/BENCH.md and results/latest.json
bench/seed_memory.py  dbt seed memory under tracemalloc, run separately
dbt_project/        the dbt project (sources, staging views, marts, tests)
docker-compose.yml  Postgres 18
results/            committed snapshot of the run described in BENCH.md
```

The dataset: `raw_orders` (N rows: id, customer, timestamp, status, amount
in cents, gift flag, nullable coupon) and `raw_customers` (N / 20 rows: id,
name, email, country, signup date). Ints, text, timestamps, dates, booleans
and NULLs, so every loader has to get types and NULLs right. It is written
as both Parquet and CSV; Parquet and CSV for the same size hold the same rows.

The dbt project: two staging views, three marts (`dim_customers`,
`fct_daily_revenue`, `fct_country_status`), 18 generic tests plus two
singular tests that reconcile revenue and row counts between raw and marts.

## Why `dbt seed` is not a loader

dbt's own docs say seeds are for "static data which changes infrequently"
(country-code mappings, lists of test emails) and list "Loading raw data that
has been exported to CSVs" as a **poor** use case
([dbt docs: seeds](https://docs.getdbt.com/docs/build/seeds)). This repo
shows why with numbers instead of a rule.

What `dbt seed` does under the hood (dbt-core 1.12 / dbt-postgres 1.11 / dbt-duckdb 1.11):

1. Reads the **whole CSV into memory** as an [agate](https://agate.readthedocs.io/)
   table: every cell becomes a Python object and goes through type inference.
2. Creates the table.
3. Loads the rows:
   * **Postgres**: renders `insert into ... values (%s, %s, ...), (...)` for
     10,000 rows at a time with bound parameters, through psycopg2. Every value
     makes a round trip through Python.
   * **DuckDB**: dbt-duckdb has a fast path (`fast: true` by default) that
     runs DuckDB's native `COPY ... FROM 'file.csv'`. Step 1 still runs first,
     so you pay the agate parse for nothing but type inference.

That is perfect for a 200-row mapping file under version control, and the
wrong tool for raw data. Seeds also live in your git repo, which is the
wrong place for production data or anything with PII.

## What ADBC and Arrow are (short version)

* **Apache Arrow** is a columnar in-memory format: a column of a million
  integers is one contiguous buffer, not a million Python objects. Parquet
  readers, DuckDB, Polars, Spark and many drivers can hand Arrow buffers to
  each other without converting them row by row.
* **ODBC/JDBC/DB-API** are row-oriented: the driver gives you rows, you give
  it rows. Even when both ends are columnar (Parquet file, columnar
  warehouse), a row API forces a transpose in the middle.
* **[ADBC](https://arrow.apache.org/adbc/)** (Arrow Database Connectivity) is
  a database API whose unit of data is an Arrow record batch.
  `cursor.adbc_ingest(table, data)` takes an Arrow table or a
  `RecordBatchReader` (a stream of batches) and lets each driver use that
  engine's fastest bulk path. The Postgres driver streams the batches with
  Postgres's binary `COPY`; the DuckDB driver hands the Arrow buffers to
  DuckDB directly. Your Python code is the same for both.

This is the same idea behind `AdbcHook` bulk ingest in Apache Airflow
([apache/airflow#76363](https://github.com/apache/airflow/pull/76363)), and
dbt Fusion talks to warehouses through ADBC drivers.

The three ADBC variants here differ only in how the Arrow data is produced:

| Loader | Arrow source | Memory behaviour |
| --- | --- | --- |
| `adbc_parquet` | `pq.read_table()`: whole file in memory | grows with file size |
| `adbc_parquet_stream` | `ParquetFile.iter_batches()` wrapped in a `RecordBatchReader` | one row group at a time |
| `adbc_csv_stream` | `pyarrow.csv.open_csv()` with explicit types | one 8 MiB block at a time |

## Methodology

* **One fresh process per measurement** (`bench/worker.py`). The target is
  reset first: schemas dropped, Postgres `CHECKPOINT` (so one run's dirty
  pages are not flushed inside the next run's timer), DuckDB file deleted.
* **Timer scope**: from opening the connection to commit, including reading
  the input file and `CREATE TABLE`. Imports, dataset generation and the
  post-load row count are outside the timer. `dbt seed` is timed through
  `dbtRunner` in-process, so its time includes dbt's project parsing, which
  is what a user waits for.
* **Rounds**: 3 timed rounds per cell, **interleaved** (round 1 runs every
  loader once in a shuffled order, then round 2, ...), median reported with
  min-max.
* **Memory** comes from runs that are not timed:
  * `Peak RSS` is the process high-water mark (`ru_maxrss`) in the timed runs;
    `+delta` is how much it grew during the load (a lower bound).
  * One extra round per cell runs under `tracemalloc` (Python heap peak) and
    reads `pyarrow`'s allocator peak (`max_memory()`). tracemalloc slows
    Python-heavy code down, which is why those runs are never used for timing.
    It does not see Arrow, DuckDB or libpq memory; RSS does.
* **`dbt seed` at 10m is not run** on either engine (`--skip dbt_seed@10m`).
  On Postgres the linear projection from 1m is ~75 min per round, x3 rounds;
  on DuckDB it was dropped as well to keep one rule for both engines and a
  bounded total runtime. Its 1m Postgres tracemalloc round was also stopped
  by hand after ~16 min; seed memory is measured separately at 100k with
  `just seed-memory` instead.
* **Timeouts**: a run over 600 s is killed. Before a bigger size, the
  previous size's median is scaled linearly; if that projection exceeds the
  timeout the cell is marked *skipped (projected)* and shows the projection
  in words, never as a measured number.
* **Same types everywhere**: Python loaders write into tables created from
  one DDL (`bench/config.py`); `dbt seed` gets the same types through
  `column_types` in `dbt_project.yml`.
* **Correctness** (`bench/verify.py`): for every loader on both engines, load,
  run `dbt build --exclude-resource-type seed`, then fingerprint the raw tables
  and every mart (row count + sha256 over ordered, normalised rows). All
  fingerprints must match across loaders, and across DuckDB and Postgres.
* **Fairness notes**: Postgres runs with `shared_buffers=512MB`,
  `max_wal_size=4GB` and default durability (`fsync=on`,
  `synchronous_commit=on`). The classic loaders read Parquet (same input as
  the Parquet ADBC loaders), so their slowness is the row path, not CSV
  parsing. `executemany` gets 128k-row batches; psycopg 3 pipelines them.

## Findings

All numbers are medians from [results/BENCH.md](results/BENCH.md). Rows/s
counts customers + orders.

**1. `dbt seed` is a row-by-row loader with a big fixed memory cost.**

* Postgres: 47.6 s at 100k and 449 s at 1m, a flat ~2.3k rows/s. A linear
  projection to 10m is ~4,500 s (~75 min) per round, so `dbt seed` was not run
  at 10m on either engine (`--skip dbt_seed@10m`; recorded as skipped, not as
  a result).
* DuckDB: 1.83 s at 100k and 7.99 s at 1m. Much better, because dbt-duckdb
  loads the file with DuckDB's native `COPY`, but it still parses the whole
  CSV with agate first: **886 MiB of Python heap** (tracemalloc) and 1.16 GiB
  peak RSS to load a 69 MB CSV. Every ADBC and DuckDB-native path stayed
  under 1 MiB of Python heap.
* Dedicated seed memory run at 100k (3 runs per engine): ~100 MiB Python heap
  on both engines for a 6.7 MB CSV. The memory cost is agate, not the
  database.

**2. Row paths are an order of magnitude or more behind.**

* Postgres `psycopg` `executemany` (batched, pipelined by psycopg 3): a flat
  ~90-95k rows/s, 1.18 s / 11.15 s / 110 s at 100k / 1m / 10m. ADBC streaming
  was 18x faster at 1m and 23x faster at 10m.
* DuckDB `executemany`: 840 rows/s (125 s for 100k). DuckDB is a columnar
  engine and per-row inserts are its worst case. 1m and 10m were skipped by
  projection (~21 min and ~3.5 h).

**3. Postgres: ADBC beats hand-written `COPY` CSV from 1m rows up.**

| Size | `psycopg_copy_csv` | `adbc_parquet_stream` | `adbc_csv_stream` |
| --- | ---: | ---: | ---: |
| 100k | **0.11 s** | 0.18 s | 0.21 s |
| 1m | 0.95 s | **0.62 s** | 0.87 s |
| 10m | 8.68 s | **4.84 s** | 5.80 s |

The ADBC Postgres driver also uses `COPY`, but in binary form built from
Arrow buffers, so the server skips CSV text parsing. At 100k the fixed cost
of the Arrow/ADBC setup dominates and plain `COPY` wins. `COPY` CSV keeps
the smallest client memory of everything here (it just streams bytes).

**4. DuckDB: the engine's own readers win on DuckDB.**

At 10m: `read_parquet` 1.38 s, Arrow replacement scan 1.53 s, `read_csv`
1.98 s, ADBC ingest 4.00-6.47 s. DuckDB reads Parquet with its own parallel
reader; ADBC ingest receives one stream of batches from Python. If you only
ever target DuckDB, use its native readers. ADBC's case is the same loader
code for DuckDB, Postgres, Snowflake, BigQuery and others.

**5. Stream, do not materialise.**

At 10m on Postgres, `adbc_parquet_stream` peaked at 518 MiB RSS against
1,093 MiB for `adbc_parquet` (`pq.read_table()` first), and was also faster
(4.84 s vs 5.36 s). Arrow pool peak: 270 vs 767 MiB. Same on DuckDB
(1,046 vs 1,621 MiB). A `RecordBatchReader` keeps memory bounded by the batch
size instead of the file size.

**6. dbt does not care how the data arrived, as long as types match.**

`bench/verify.py` at 100k: 14 load paths x `dbt build` = 14 passes
(5 models, 18 generic + 2 singular tests each). Raw tables and all three marts
had one sha256 per relation across every load path and across both engines.
This holds because every loader writes the same types (one DDL, and
`column_types` for seeds). Exercise 2 shows what happens when you let agate
infer types instead.

## What this does NOT prove

* **Not a warehouse benchmark.** DuckDB is embedded and Postgres runs in a
  container on the same machine, so there is no network latency. Over a real
  network the row paths get slower (more round trips) and the gap usually
  grows, but that was not measured here.
* **Not Snowflake, BigQuery, Databricks or Athena.** Their bulk paths stage
  files and run server-side loads; costs, warehouse sizing and latency
  dominate. See [docs/snowflake.md](docs/snowflake.md). Do not extrapolate.
* **Not "ADBC is always fastest".** Engine-native paths (`COPY`,
  `read_parquet`) are as fast or faster on their own engine. ADBC's point is
  that one engine-neutral API gets you into that league without writing
  per-engine code; it is not magic.
* **Not dbt runtime.** `dbt build` time is the same whichever path loaded
  the data (that is the point of the verify step); it is reported for
  context only.
* **Not a quiet machine.** The snapshot ran on a shared 8 vCPU VM with other
  workloads running; load average is recorded in `results/latest.json`.
  Look at ratios and orders of magnitude, not the third digit. Re-run it on
  your machine.
* **Not every loader tuning.** Each loader uses common, documented settings.
  Someone can make any single path faster (bigger batches, `UNLOGGED`
  tables, parallel workers); PRs that keep the comparison fair are welcome.
* **Not wide or nested data.** Seven narrow columns, no JSON, no decimals with
  scale, no very long strings.

## Exercises (for students)

1. **Read the loaders.** Open `bench/loaders.py`. For `psycopg_executemany`,
   how many Python objects are created per row? For `adbc_parquet_stream`?
2. **Break `dbt seed`.** Delete the `column_types` block in
   `dbt_project.yml`, run `just verify 10k`, and look at what agate inferred
   for `coupon_code` and `order_ts`. Do the fingerprints still match?
3. **Memory.** Compare `adbc_parquet` and `adbc_parquet_stream` in the
   `Peak RSS` column at 10m. Explain the difference in one sentence.
4. **Scale it.** Run `uv run python -m bench.run --targets duckdb --sizes 100k,1m --rounds 3`.
   Plot rows/s against size for each loader. Which ones are flat, and why?
5. **New engine.** Add SQLite with `adbc-driver-sqlite`: a `_sqlite_create_tables`
   helper and one `Loader(...)` line. How much of `adbc_ingest()` did you have
   to change?
6. **Incremental.** Make `fct_daily_revenue` an incremental model and load a
   second day of orders with `mode="append"`. What changes in the verify step?

## License

Apache-2.0.
