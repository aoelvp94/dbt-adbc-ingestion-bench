-- Net revenue must be identical whether summed per day or per customer.
-- Returns rows (= failure) only if the two marts disagree.
with by_day as (
    select sum(net_revenue_cents) as cents from {{ ref('fct_daily_revenue') }}
),
by_customer as (
    select sum(net_revenue_cents) as cents from {{ ref('dim_customers') }}
)
select by_day.cents as day_cents, by_customer.cents as customer_cents
from by_day cross join by_customer
where by_day.cents <> by_customer.cents
