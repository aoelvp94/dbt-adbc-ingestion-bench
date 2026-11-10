# Snowflake (documented, not run)

This repo never connects to Snowflake: there are no credentials in it and CI
or a reviewer should not need an account. The same experiment maps onto
Snowflake like this if you want to run it yourself.

| Path in this repo | Snowflake equivalent |
| --- | --- |
| `dbt_seed` | `dbt seed` with `dbt-snowflake` (agate + batched `INSERT`, same design) |
| `psycopg_executemany` / `dbapi_executemany` | `cursor.executemany` via `snowflake-connector-python` (row path) |
| `psycopg_copy_csv` (expert) | `PUT` the file to a stage + `COPY INTO` (server-side bulk load) |
| `adbc_*` | `adbc-driver-snowflake`: `cursor.adbc_ingest(...)` stages Parquet and runs `COPY INTO` for you |

Sketch (not executed here):

```python
import adbc_driver_snowflake.dbapi as sf
import pyarrow.parquet as pq

with sf.connect(uri="user:password@account/database/schema?warehouse=WH&role=ROLE") as conn:
    with conn.cursor() as cur:
        reader = pq.ParquetFile("data/1m/raw_orders.parquet").iter_batches(batch_size=128_000)
        # build a RecordBatchReader exactly like bench/loaders.py does
        cur.adbc_ingest("RAW_ORDERS", reader, mode="append", db_schema_name="RAW")
    conn.commit()
```

Things that change the numbers on Snowflake and would need their own
measurement: warehouse size and auto-resume, network latency to the region,
stage upload bandwidth, and per-query credits. Do not extrapolate the
DuckDB/Postgres numbers to Snowflake.
