select
    customer_id,
    full_name,
    lower(email) as email,
    country as country_code,
    signup_date
from {{ source('raw', 'raw_customers') }}
