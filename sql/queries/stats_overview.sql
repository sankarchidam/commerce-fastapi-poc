-- name: stats_overview
-- Counts come straight from core; money comes from the order_metrics metric view, so this
-- endpoint, GET /metrics and any dashboard share one definition of GMV and refunds.
WITH m AS (
  SELECT
    MEASURE(gmv)             AS gmv,
    MEASURE(refunded_amount) AS refunded_amount,
    MEASURE(gmv_usd)         AS gmv_usd,
    MEASURE(net_revenue_usd) AS net_revenue_usd
  FROM {metrics}.order_metrics
)
SELECT
  (SELECT COUNT(*) FROM {core}.customers) AS customers,
  (SELECT COUNT(*) FROM {core}.items) AS items,
  (SELECT COUNT(*) FROM {core}.orders) AS orders,
  (SELECT COUNT(*) FROM {core}.refunds) AS refunds,
  COALESCE(m.gmv, 0) AS gmv,
  COALESCE(m.refunded_amount, 0) AS refunded_amount,
  COALESCE(m.gmv_usd, 0) AS gmv_usd,
  COALESCE(m.net_revenue_usd, 0) AS net_revenue_usd,
  (SELECT MAX(received_at) FROM {raw}.webhook_events) AS last_webhook_at
FROM m
