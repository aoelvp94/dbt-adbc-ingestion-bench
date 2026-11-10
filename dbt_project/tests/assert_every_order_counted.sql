-- Every raw order must land in exactly one daily bucket.
with raw_count as (select count(*) as n from {{ source('raw', 'raw_orders') }}),
mart_count as (select sum(orders) as n from {{ ref('fct_daily_revenue') }})
select raw_count.n, mart_count.n
from raw_count cross join mart_count
where raw_count.n <> mart_count.n
