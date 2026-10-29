-- name: order_refunds
SELECT r.refund_id, r.order_id, o.order_number, r.provider, r.provider_refund_id, r.amount, r.currency,
       r.reason, r.status, r.refunded_at, r.note
FROM {core}.refunds r
LEFT JOIN {core}.orders o ON o.order_id = r.order_id
WHERE r.order_id = :order_id
ORDER BY r.refunded_at
