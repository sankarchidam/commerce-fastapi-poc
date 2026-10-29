from __future__ import annotations

import base64
import hashlib
import hmac

from tests.conftest import SECRET, load_fixture


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def post(client, topic_path: str, body: bytes, *, webhook_id: str = "wh-1", signature: str | None = None, topic_header: str | None = None):
    headers = {
        "X-Shopify-Hmac-Sha256": signature if signature is not None else sign(body),
        "X-Shopify-Webhook-Id": webhook_id,
        "X-Shopify-Shop-Domain": "demo.myshopify.com",
        "Content-Type": "application/json",
    }
    if topic_header:
        headers["X-Shopify-Topic"] = topic_header
    return client.post(f"/webhooks/shopify/{topic_path}", content=body, headers=headers)


def test_invalid_hmac_is_rejected_and_never_applied(make_client, backend):
    client = make_client()
    body = load_fixture("orders_create.json")
    resp = post(client, "orders_create", body, signature=sign(body, "wrong-secret"))
    assert resp.status_code == 401
    assert backend.core.applied == []
    # The attempt is logged under a generated id, never the sender-supplied one.
    assert "wh-1" not in backend.raw.events
    [event] = backend.raw.events.values()
    assert event["hmac_valid"] is False and event["processing_status"] == "failed"


def test_missing_hmac_is_rejected(make_client, backend):
    client = make_client()
    body = load_fixture("orders_create.json")
    resp = client.post("/webhooks/shopify/orders_create", content=body)
    assert resp.status_code == 401
    assert backend.core.applied == []


def test_tampered_body_is_rejected(make_client, backend):
    client = make_client()
    body = load_fixture("orders_create.json")
    resp = post(client, "orders_create", body.replace(b"64.52", b"0.01"), signature=sign(body))
    assert resp.status_code == 401
    assert backend.core.applied == []


def test_valid_hmac_applies_once_and_replay_is_duplicate(make_client, backend):
    client = make_client()
    body = load_fixture("orders_create.json")

    first = post(client, "orders_create", body, webhook_id="wh-abc")
    assert first.status_code == 200
    assert first.json()["status"] == "applied"
    assert first.json()["topic"] == "orders/create"
    assert first.json()["applied"]["orders"] == 1
    assert first.json()["applied"]["order_details"] == 2
    assert backend.raw.events["wh-abc"]["processing_status"] == "applied"
    assert backend.raw.events["wh-abc"]["hmac_valid"] is True

    replay = post(client, "orders_create", body, webhook_id="wh-abc")
    assert replay.status_code == 200
    assert replay.json() == {"status": "duplicate", "event_id": "wh-abc", "topic": "orders/create", "applied": {}, "error": None}
    assert len(backend.core.applied) == 1  # not double-applied


def test_failed_event_is_reprocessed_on_retry(make_client, backend):
    client = make_client()
    refund = load_fixture("refunds_create.json")
    # Refund arrives before its order -> stored, failed, 500 so Shopify retries.
    r1 = post(client, "refunds_create", refund, webhook_id="wh-refund")
    assert r1.status_code == 500 and r1.json()["status"] == "failed"
    assert backend.raw.events["wh-refund"]["processing_status"] == "failed"

    post(client, "orders_create", load_fixture("orders_create.json"), webhook_id="wh-order")
    r2 = post(client, "refunds_create", refund, webhook_id="wh-refund")
    assert r2.status_code == 200 and r2.json()["status"] == "applied"
    assert r2.json()["applied"] == {"refunds": 1, "refund_details": 1}


def test_header_topic_overrides_path(make_client, backend):
    client = make_client()
    body = load_fixture("products_create.json")
    resp = post(client, "orders_create", body, topic_header="products/create")
    assert resp.status_code == 200
    assert resp.json()["topic"] == "products/create"
    assert set(resp.json()["applied"]) == {"items", "item_variants"}


def test_unknown_topic_is_stored_but_skipped(make_client, backend):
    client = make_client()
    body = load_fixture("unknown_topic.json")
    resp = post(client, "app_uninstalled", body, webhook_id="wh-unknown")
    assert resp.status_code == 202
    assert resp.json()["status"] == "skipped"
    assert backend.raw.events["wh-unknown"]["processing_status"] == "skipped"
    assert backend.core.applied == []


def test_invalid_topic_path_is_400(make_client):
    client = make_client()
    body = b"{}"
    assert post(client, "orders", body).status_code == 400
    assert post(client, "orders_create", body, topic_header="orders/create; DROP").status_code == 400


def test_missing_webhook_id_dedupes_on_body(make_client, backend):
    client = make_client()
    body = load_fixture("customers_create.json")
    headers = {"X-Shopify-Hmac-Sha256": sign(body), "X-Shopify-Topic": "customers/create"}
    a = client.post("/webhooks/shopify/customers_create", content=body, headers=headers)
    b = client.post("/webhooks/shopify/customers_create", content=body, headers=headers)
    assert a.json()["status"] == "applied"
    assert b.json()["status"] == "duplicate"
    assert a.json()["event_id"].startswith("gen_")


def test_background_mode_acks_after_landing_then_applies(make_client, backend, settings, monkeypatch):
    monkeypatch.setattr(settings, "webhook_apply_mode", "background")
    client = make_client()
    body = load_fixture("orders_create.json")
    resp = post(client, "orders_create", body, webhook_id="wh-bg")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    # TestClient runs background tasks before returning, so the apply has happened.
    assert backend.raw.events["wh-bg"]["processing_status"] == "applied"
    assert len(backend.core.applied) == 1
    assert post(client, "orders_create", body, webhook_id="wh-bg").json()["status"] == "duplicate"
