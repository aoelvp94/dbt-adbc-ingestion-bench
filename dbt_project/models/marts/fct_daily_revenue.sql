select
    order_date,
    cast(count(*) as bigint) as orders,
    cast(sum(case when is_gift then 1 else 0 end) as bigint) as gift_orders,
    cast(sum(case when has_coupon then 1 else 0 end) as bigint) as coupon_orders,
    cast(sum(case when is_refunded then 1 else 0 end) as bigint) as refunded_orders,
    cast(sum(case when not is_refunded then amount_cents else 0 end) as bigint) as net_revenue_cents
from {{ ref('stg_orders') }}
group by order_date
