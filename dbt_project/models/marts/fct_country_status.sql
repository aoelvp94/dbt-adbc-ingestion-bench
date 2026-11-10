select
    c.country_code,
    o.status,
    cast(count(*) as bigint) as orders,
    cast(sum(o.amount_cents) as bigint) as gross_cents
from {{ ref('stg_orders') }} as o
join {{ ref('stg_customers') }} as c on o.customer_id = c.customer_id
group by c.country_code, o.status
