-- name: customers_get
SELECT
  c.customer_id, c.provider, c.provider_customer_id, c.email, c.first_name, c.last_name, c.phone,
  c.region, c.country, c.segment, c.marketing_opt_in, c.status, c.created_at, c.updated_at,
  l.order_count, l.gross, l.refunds, l.net, l.first_order_at, l.last_order_at
FROM {core}.customers c
LEFT JOIN {core}.v_customer_ltv l ON l.customer_id = c.customer_id
WHERE c.customer_id = :customer_id
