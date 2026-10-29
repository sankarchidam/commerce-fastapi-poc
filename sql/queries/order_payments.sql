-- name: order_payments
SELECT payment_id, provider, provider_payment_id, method, amount, currency, status, processed_at
FROM {core}.payments
WHERE order_id = :order_id
ORDER BY processed_at
