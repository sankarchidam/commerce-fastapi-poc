-- name: orders_list
-- date_from / date_to are inclusive calendar days (UTC).
SELECT
  o.order_id, o.provider, o.provider_order_id, o.customer_id, c.email AS customer_email, o.order_number,
  o.status, o.financial_status, o.fulfillment_status, o.currency,
  o.subtotal, o.tax, o.shipping, o.discount, o.total, o.source,
  COALESCE(d.item_count, 0) AS item_count,
  COALESCE(r.refunded_amount, 0) AS refunded_amount,
  o.ordered_at, o.cancelled_at, o.updated_at
FROM {core}.orders o
LEFT JOIN {core}.customers c ON c.customer_id = o.customer_id
LEFT JOIN (SELECT order_id, SUM(quantity) AS item_count FROM {core}.order_details GROUP BY order_id) d
  ON d.order_id = o.order_id
LEFT JOIN (SELECT order_id, SUM(amount) AS refunded_amount FROM {core}.refunds WHERE status = 'succeeded' GROUP BY order_id) r
  ON r.order_id = o.order_id
WHERE (:customer_id IS NULL OR o.customer_id = :customer_id)
  AND (:status IS NULL OR o.status = :status)
  AND (:financial_status IS NULL OR o.financial_status = :financial_status)
  AND (:provider_order_id IS NULL OR o.provider_order_id = :provider_order_id)
  AND (:date_from IS NULL OR o.ordered_at >= CAST(:date_from AS DATE))
  AND (:date_to IS NULL OR o.ordered_at < date_add(CAST(:date_to AS DATE), 1))
