with orders as (
    select
        customer_id,
        count(*) as order_count,
        sum(case when not is_refunded then amount_cents else 0 end) as net_revenue_cents,
        min(order_ts) as first_order_ts,
        max(order_ts) as last_order_ts
    from {{ ref('stg_orders') }}
    group by customer_id
)

select
    c.customer_id,
    c.country_code,
    c.signup_date,
    cast(coalesce(o.order_count, 0) as bigint) as order_count,
    cast(coalesce(o.net_revenue_cents, 0) as bigint) as net_revenue_cents,
    o.first_order_ts,
    o.last_order_ts
from {{ ref('stg_customers') }} as c
left join orders as o on c.customer_id = o.customer_id
