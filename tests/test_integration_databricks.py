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


def test_orders_roundtrip(client):
    from app.config import get_settings

    headers = {"X-API-Key": get_settings().api_key}
    page = client.get("/orders?limit=5", headers=headers).json()
    assert page["total"] >= 5000 and len(page["items"]) == 5
    detail = client.get(f"/orders/{page['items'][0]['order_id']}", headers=headers).json()
    assert detail["lines"]
