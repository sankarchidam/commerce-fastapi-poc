-- name: order_lines
SELECT order_detail_id, item_id, variant_id, sku, title, quantity, unit_price, line_discount,
       line_tax, line_total, fulfillment_status
FROM {core}.order_details
WHERE order_id = :order_id
ORDER BY order_detail_id
