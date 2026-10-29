-- name: order_fulfillments
SELECT fulfillment_id, status, tracking_company, tracking_number, shipped_at, delivered_at
FROM {core}.fulfillments
WHERE order_id = :order_id
ORDER BY shipped_at
