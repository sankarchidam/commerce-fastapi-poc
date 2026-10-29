-- name: refunds_list
SELECT r.refund_id, r.order_id, o.order_number, r.provider, r.provider_refund_id, r.amount, r.currency,
       r.reason, r.status, r.refunded_at, r.note
FROM {core}.refunds r
LEFT JOIN {core}.orders o ON o.order_id = r.order_id
WHERE (:order_id IS NULL OR r.order_id = :order_id)
  AND (:status IS NULL OR r.status = :status)
  AND (:date_from IS NULL OR r.refunded_at >= CAST(:date_from AS DATE))
  AND (:date_to IS NULL OR r.refunded_at < date_add(CAST(:date_to AS DATE), 1))
