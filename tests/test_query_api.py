from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.conftest import API_KEY

AUTH = {"X-API-Key": API_KEY}
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def order_row(i: int) -> dict:
    return {
        "order_id": f"ord_{i:04d}", "provider": "shopify", "provider_order_id": f"gid://shopify/Order/{i}",
        "customer_id": "cust_1", "customer_email": "a@example.com", "order_number": f"#{1000 + i}",
        "status": "paid", "financial_status": "paid", "fulfillment_status": "unfulfilled", "currency": "USD",
        "subtotal": Decimal("20.00"), "tax": Decimal("1.40"), "shipping": Decimal("5.99"),
        "discount": Decimal("0.00"), "total": Decimal("27.39"), "source": "online",
        "item_count": 2, "refunded_amount": Decimal("0"), "ordered_at": BASE - timedelta(hours=i),
        "cancelled_at": None, "updated_at": BASE - timedelta(hours=i),
    }


ORDERS = [order_row(i) for i in range(120)]


def test_requires_api_key(make_client):
    client = make_client({"orders_list": lambda p: ORDERS})
    assert client.get("/orders").status_code == 401
    assert client.get("/orders", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/health").status_code == 200  # liveness stays open


def test_order_list_pagination(make_client):
    client = make_client({"orders_list": lambda p: ORDERS})

    first = client.get("/orders", headers=AUTH).json()
    assert first["total"] == 120 and first["limit"] == 50 and first["offset"] == 0
    assert len(first["items"]) == 50
    assert first["items"][0]["order_id"] == "ord_0000"

    last = client.get("/orders?limit=50&offset=100", headers=AUTH).json()
    assert last["total"] == 120 and len(last["items"]) == 20
    assert last["items"][0]["order_id"] == "ord_0100"

    beyond = client.get("/orders?offset=500", headers=AUTH).json()
    assert beyond == {"items": [], "total": 120, "limit": 50, "offset": 500}

    assert client.get("/orders?limit=201", headers=AUTH).status_code == 422
    assert client.get("/orders?limit=0", headers=AUTH).status_code == 422
    assert client.get("/orders?offset=-1", headers=AUTH).status_code == 422


def test_order_list_filters_are_bound_parameters(make_client):
    client = make_client({"orders_list": lambda p: ORDERS[:3]})
    evil = "x' OR '1'='1"
    resp = client.get("/orders", params={"customer_id": evil, "date_from": "2026-01-01", "status": "paid"}, headers=AUTH)
    assert resp.status_code == 200
    session = client.app.state.fake_session
    for name, params, sql in session.calls:
        assert evil not in sql
        assert params["customer_id"] == evil
        assert params["date_from"] == "2026-01-01"
    assert ":customer_id" in session.calls[0][2]
    # Enum filters are validated before reaching SQL.
    assert client.get("/orders?status=bogus", headers=AUTH).status_code == 422


def test_order_list_money_and_timestamps_are_stable(make_client):
    client = make_client({"orders_list": lambda p: ORDERS[:1]})
    item = client.get("/orders", headers=AUTH).json()["items"][0]
    assert item["total"] == "27.39"
    assert item["refunded_amount"] == "0.00"
    assert item["ordered_at"] == "2026-10-01T12:00:00Z"


def test_order_detail_shape(make_client):
    header = {**order_row(1), "shipping_address_id": "addr_s", "billing_address_id": "addr_b",
              "refunded_amount": Decimal("8.67"), "status": "partially_refunded"}
    handlers = {
        "orders_get": lambda p: [header] if p["order_id"] == "ord_0001" else [],
        "order_lines": lambda p: [
            {"order_detail_id": "od_1", "item_id": "item_1", "variant_id": "var_1", "sku": "HC-0001-1",
             "title": "Lavender Shampoo - 250 ml", "quantity": 2, "unit_price": Decimal("9.99"),
             "line_discount": Decimal("0"), "line_tax": Decimal("1.40"), "line_total": Decimal("21.38"),
             "fulfillment_status": "fulfilled"},
        ],
        "order_payments": lambda p: [
            {"payment_id": "pay_1", "provider": "shopify", "provider_payment_id": None, "method": "paypal",
             "amount": Decimal("27.39"), "currency": "USD", "status": "captured", "processed_at": BASE},
        ],
        "order_fulfillments": lambda p: [
            {"fulfillment_id": "ful_1", "status": "success", "tracking_company": "UPS",
             "tracking_number": "1Z999", "shipped_at": BASE, "delivered_at": None},
        ],
        "order_refunds": lambda p: [
            {"refund_id": "ref_1", "order_id": "ord_0001", "order_number": "#1001", "provider": "shopify",
             "provider_refund_id": "gid://shopify/Refund/1", "amount": Decimal("8.67"), "currency": "USD",
             "reason": "customer", "status": "succeeded", "refunded_at": BASE, "note": "n"},
        ],
        "order_refund_lines": lambda p: [
            {"refund_id": "ref_1", "refund_detail_id": "rd_1", "order_detail_id": "od_1", "quantity": 1,
             "amount": Decimal("8.67"), "restock": True},
        ],
    }
    client = make_client(handlers)
    resp = client.get("/orders/ord_0001", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"order_id", "status", "total", "lines", "payments", "fulfillments", "refunds",
                         "shipping_address_id", "billing_address_id", "refunded_amount", "item_count"}
    assert body["lines"][0] == {
        "order_detail_id": "od_1", "item_id": "item_1", "variant_id": "var_1", "sku": "HC-0001-1",
        "title": "Lavender Shampoo - 250 ml", "quantity": 2, "unit_price": "9.99", "line_discount": "0.00",
        "line_tax": "1.40", "line_total": "21.38", "fulfillment_status": "fulfilled",
    }
    assert body["payments"][0]["amount"] == "27.39"
    assert body["fulfillments"][0]["tracking_company"] == "UPS"
    assert body["refunds"][0]["lines"][0]["restock"] is True
    assert body["refunded_amount"] == "8.67"

    assert client.get("/orders/ord_missing", headers=AUTH).status_code == 404


def test_customer_search_escapes_like_wildcards(make_client):
    client = make_client({"customers_list": lambda p: []})
    client.get("/customers", params={"q": "100%_Off"}, headers=AUTH)
    _, params, _ = client.app.state.fake_session.calls[0]
    assert params["q"] == "%100\\%\\_off%"


def test_stats_overview(make_client):
    client = make_client({"stats_overview": lambda p: [{
        "customers": 500, "items": 200, "orders": 5000, "refunds": 480,
        "gmv": Decimal("312345.678"), "refunded_amount": Decimal("9876.5"),
        "gmv_usd": Decimal("280000.123"), "net_revenue_usd": Decimal("271000"), "last_webhook_at": None,
    }]})
    body = client.get("/stats/overview", headers=AUTH).json()
    assert body == {"customers": 500, "items": 200, "orders": 5000, "refunds": 480,
                    "gmv": "312345.68", "refunded_amount": "9876.50", "gmv_usd": "280000.12",
                    "net_revenue_usd": "271000.00", "last_webhook_at": None}
    # Money comes from the metric view, not a hand-written SUM.
    sql = client.app.state.fake_session.calls[0][2]
    assert "MEASURE(gmv)" in sql and ".`metrics`.order_metrics" in sql
