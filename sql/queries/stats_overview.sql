-- name: stats_overview
-- GMV = sum of order totals excluding cancelled orders. Currencies are summed as-is (POC).
SELECT
  (SELECT COUNT(*) FROM {core}.customers) AS customers,
  (SELECT COUNT(*) FROM {core}.items) AS items,
  (SELECT COUNT(*) FROM {core}.orders) AS orders,
  (SELECT COUNT(*) FROM {core}.refunds) AS refunds,
  (SELECT COALESCE(SUM(total), 0) FROM {core}.orders WHERE status <> 'cancelled') AS gmv,
  (SELECT COALESCE(SUM(amount), 0) FROM {core}.refunds WHERE status = 'succeeded') AS refunded_amount,
  (SELECT MAX(received_at) FROM {raw}.webhook_events) AS last_webhook_at
