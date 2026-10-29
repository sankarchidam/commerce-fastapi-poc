-- name: refunds_get
SELECT r.refund_id, r.order_id, o.order_number, r.provider, r.provider_refund_id, r.amount, r.currency,
       r.reason, r.status, r.refunded_at, r.note
FROM {core}.refunds r
LEFT JOIN {core}.orders o ON o.order_id = r.order_id
WHERE r.refund_id = :refund_id
