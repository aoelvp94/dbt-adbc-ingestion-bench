select
    order_id,
    customer_id,
    order_ts,
    cast(order_ts as date) as order_date,
    status,
    amount_cents,
    cast(amount_cents as numeric(18, 2)) / 100 as amount,
    is_gift,
    coupon_code,
    coupon_code is not null as has_coupon,
    status in ('returned', 'cancelled') as is_refunded
from {{ source('raw', 'raw_orders') }}
