-- name: items_get
SELECT
  i.item_id, i.provider, i.provider_product_id, i.provider_variant_id, i.sku, i.name, i.description,
  i.category, i.brand, i.status, i.created_at, i.updated_at,
  COALESCE((SELECT SUM(d.quantity)
            FROM {core}.order_details d
            JOIN {core}.orders o ON o.order_id = d.order_id
            WHERE d.item_id = i.item_id
              AND o.status <> 'cancelled'
              AND o.ordered_at >= current_timestamp() - INTERVAL 30 DAYS), 0) AS units_sold_30d
FROM {core}.items i
WHERE i.item_id = :item_id
