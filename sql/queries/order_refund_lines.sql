-- name: order_refund_lines
-- All refund lines for every refund of one order (grouped by refund_id in Python).
SELECT rd.refund_id, rd.refund_detail_id, rd.order_detail_id, rd.quantity, rd.amount, rd.restock
FROM {core}.refund_details rd
JOIN {core}.refunds r ON r.refund_id = rd.refund_id
WHERE r.order_id = :order_id
ORDER BY rd.refund_detail_id
