-- name: item_variants
SELECT variant_id, item_id, provider_variant_id, sku, title, price, currency, cost,
       inventory_quantity, weight_grams, barcode, status
FROM {core}.item_variants
WHERE item_id = :item_id
ORDER BY sku
