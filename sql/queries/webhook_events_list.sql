-- name: webhook_events_list
SELECT event_id, provider, topic, shop_domain, hmac_valid, processing_status, error_message,
       length(payload_json) AS payload_bytes, received_at, processed_at
FROM {raw}.webhook_events
WHERE (:topic IS NULL OR topic = :topic)
  AND (:status IS NULL OR processing_status = :status)
