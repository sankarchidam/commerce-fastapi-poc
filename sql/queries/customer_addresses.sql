-- name: customer_addresses
SELECT address_id, type, line1, city, state, postal_code, country, is_default
FROM {core}.addresses
WHERE customer_id = :customer_id
ORDER BY type DESC, is_default DESC, address_id
