-- name: refund_lines
SELECT refund_id, refund_detail_id, order_detail_id, quantity, amount, restock
FROM {core}.refund_details
WHERE refund_id = :refund_id
ORDER BY refund_detail_id
