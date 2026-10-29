-- name: orders_get
SELECT
  o.order_id, o.provider, o.provider_order_id, o.customer_id, c.email AS customer_email, o.order_number,
  o.status, o.financial_status, o.fulfillment_status, o.currency,
  o.subtotal, o.tax, o.shipping, o.discount, o.total, o.source,
  COALESCE((SELECT SUM(quantity) FROM {core}.order_details d WHERE d.order_id = o.order_id), 0) AS item_count,
  COALESCE((SELECT SUM(amount) FROM {core}.refunds r WHERE r.order_id = o.order_id AND r.status = 'succeeded'), 0) AS refunded_amount,
  o.ordered_at, o.cancelled_at, o.updated_at, o.shipping_address_id, o.billing_address_id
FROM {core}.orders o
LEFT JOIN {core}.customers c ON c.customer_id = o.customer_id
WHERE o.order_id = :order_id
