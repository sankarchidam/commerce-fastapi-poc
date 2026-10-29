-- name: items_list
SELECT
  i.item_id, i.provider, i.provider_product_id, i.provider_variant_id, i.sku, i.name, i.description,
  i.category, i.brand, i.status, i.created_at, i.updated_at
FROM {core}.items i
WHERE (:category IS NULL OR i.category = :category)
  AND (:status IS NULL OR i.status = :status)
  AND (:q IS NULL
       OR lower(i.name) LIKE :q
       OR lower(i.sku) LIKE :q
       OR EXISTS (SELECT 1 FROM {core}.item_variants v WHERE v.item_id = i.item_id AND lower(v.sku) LIKE :q))
