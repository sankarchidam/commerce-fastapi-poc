-- name: webhook_events_get
SELECT event_id, provider, topic, shop_domain, hmac_valid, processing_status, error_message,
       length(payload_json) AS payload_bytes, payload_json, received_at, processed_at
FROM {raw}.webhook_events
WHERE event_id = :event_id
