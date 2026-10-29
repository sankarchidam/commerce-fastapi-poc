"""Optional: runs against a real, bootstrapped warehouse.

    RUN_DATABRICKS_INTEGRATION=1 pytest -m integration
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_DATABRICKS_INTEGRATION") != "1", reason="set RUN_DATABRICKS_INTEGRATION=1"),
]


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c


def test_health_databricks(client):
    from app.config import get_settings

    resp = client.get("/health/databricks", headers={"X-API-Key": get_settings().api_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["core_schema_visible"] and "orders" in body["core_tables"]
    assert "order_metrics" in body["metric_views"]


def test_orders_roundtrip(client):
    from app.config import get_settings

    headers = {"X-API-Key": get_settings().api_key}
    page = client.get("/orders?limit=5", headers=headers).json()
    assert page["total"] >= 5000 and len(page["items"]) == 5
    detail = client.get(f"/orders/{page['items'][0]['order_id']}", headers=headers).json()
    assert detail["lines"]


def _headers():
    from app.config import get_settings

    return {"X-API-Key": get_settings().api_key}


def _totals(client, view: str, measures: list[str]) -> dict:
    resp = client.get(f"/metrics/{view}", params={"measures": ",".join(measures)}, headers=_headers())
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["rows"]
    return row


def test_every_measure_and_dimension_evaluates(client):
    from app.metrics import load_views

    for view in load_views().values():
        row = _totals(client, view.name, list(view.measures))
        assert set(row) == set(view.measures)
        resp = client.get(f"/metrics/{view.name}", headers=_headers(),
                          params={"measures": next(iter(view.measures)), "dimensions": ",".join(view.dimensions), "limit": 5})
        assert resp.status_code == 200, f"{view.name}: {resp.text}"


def test_metric_views_reconcile(client):
    orders = _totals(client, "order_metrics", ["gmv", "shipping", "refunded_amount", "net_revenue_usd", "valid_orders"])
    lines = _totals(client, "sales_line_metrics", ["line_revenue", "orders"])
    refunds = _totals(client, "refund_metrics", ["refunded_amount"])
    customers = _totals(client, "customer_metrics", ["ltv_usd"])
    # total = subtotal - discount + tax + shipping and sum(line_total) = subtotal - discount + tax
    assert abs(lines["line_revenue"] - (orders["gmv"] - orders["shipping"])) < 0.05
    assert lines["orders"] == orders["valid_orders"]
    assert abs(refunds["refunded_amount"] - orders["refunded_amount"]) < 0.05
    # v_customer_ltv rounds per customer, so allow a few cents per customer.
    assert abs(customers["ltv_usd"] - orders["net_revenue_usd"]) < 10


def test_stats_overview_matches_metric_view(client):
    stats = client.get("/stats/overview", headers=_headers()).json()
    orders = _totals(client, "order_metrics", ["gmv", "gmv_usd"])
    assert abs(float(stats["gmv"]) - orders["gmv"]) < 0.01
    assert abs(float(stats["gmv_usd"]) - orders["gmv_usd"]) < 0.01


def test_metric_filters_and_dates(client):
    resp = client.get("/metrics/order_metrics", headers=_headers(), params={
        "measures": "valid_orders,gmv_usd", "dimensions": "order_month,region",
        "filter": ["region:West", "region:East"], "date_from": "2026-01-01", "date_to": "2026-06-30",
        "order_by": "gmv_usd", "desc": "true",
    })
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert rows and {r["region"] for r in rows} <= {"West", "East"}
    assert all("2026-01-01" <= r["order_month"] <= "2026-06-01" for r in rows)
    assert [r["gmv_usd"] for r in rows] == sorted((r["gmv_usd"] for r in rows), reverse=True)
