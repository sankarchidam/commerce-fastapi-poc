-- name: customers_list
-- Filters are optional: a NULL parameter disables its predicate.
SELECT
  c.customer_id, c.provider, c.provider_customer_id, c.email, c.first_name, c.last_name, c.phone,
  c.region, c.country, c.segment, c.marketing_opt_in, c.status, c.created_at, c.updated_at
FROM {core}.customers c
WHERE (:email IS NULL OR lower(c.email) = lower(:email))
  AND (:region IS NULL OR c.region = :region)
  AND (:segment IS NULL OR c.segment = :segment)
  AND (:q IS NULL
       OR lower(c.email) LIKE :q
       OR lower(concat_ws(' ', c.first_name, c.last_name)) LIKE :q)
