from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.ingest import build_merge
from app.services.shopify_mapper import MappingError, map_customer, map_order, map_product, map_refund
from app.tables import make_id
from tests.conftest import MemoryCoreWriter, fixture_json


def by_table(changes):
    return {u.table: u for u in changes.upserts}


def test_order_mapping_totals_and_ids():
    changes = map_order(fixture_json("orders_create.json"), MemoryCoreWriter())
    t = by_table(changes)
    [order] = t["orders"].rows
    lines = t["order_details"].rows
    assert order["order_id"] == make_id("ord", "shopify", "gid://shopify/Order/5699000000001")
    assert order["order_number"] == "#9001"
    assert order["status"] == "paid" and order["fulfillment_status"] == "unfulfilled"
    assert order["subtotal"] == Decimal("67.00")
    assert order["discount"] == Decimal("6.70")
    assert order["tax"] == Decimal("4.22")
    assert order["total"] == Decimal("64.52")  # matches Shopify total_price
    assert order["total"] == sum(l["line_total"] for l in lines) + order["shipping"]
    assert {l["order_id"] for l in lines} == {order["order_id"]}
    assert order["customer_id"] == t["customers"].rows[0]["customer_id"]
    assert t["payments"].rows[0]["status"] == "captured"
    assert t["payments"].rows[0]["method"] == "shopify_payments"
    # FK safety: unseen products get insert-only stubs.
    assert t["items"].mode == "insert_only" and t["item_variants"].mode == "insert_only"
    assert {l["variant_id"] for l in lines} <= {v["variant_id"] for v in t["item_variants"].rows}


def test_order_maps_to_existing_customer_by_email():
    lookups = MemoryCoreWriter()
    lookups.customers["harper.lindqvist@example.com"] = "cust_existing"
    t = by_table(map_order(fixture_json("orders_create.json"), lookups))
    assert t["orders"].rows[0]["customer_id"] == "cust_existing"
    assert t["addresses"].rows[0]["customer_id"] == "cust_existing"


def test_order_updated_derives_fulfilled_status():
    t = by_table(map_order(fixture_json("orders_updated.json"), MemoryCoreWriter()))
    assert t["orders"].rows[0]["status"] == "fulfilled"
    assert t["fulfillments"].rows[0]["tracking_company"] == "USPS"
    assert t["orders"].guard == "updated_at"
    # No transactions in the payload -> one payment synthesized, inserted only if the order has none.
    assert t["payments"].key == "order_id" and t["payments"].mode == "insert_only"
    assert t["payments"].rows[0]["payment_id"] == make_id("pay", "shopify", "gid://shopify/Order/5699000000001", "1")


def test_refund_requires_known_order():
    with pytest.raises(MappingError):
        map_refund(fixture_json("refunds_create.json"), MemoryCoreWriter())


def test_refund_mapping():
    lookups = MemoryCoreWriter()
    order_id = make_id("ord", "shopify", "gid://shopify/Order/5699000000001")
    lookups.orders.add(order_id)
    changes = map_refund(fixture_json("refunds_create.json"), lookups)
    t = by_table(changes)
    [refund] = t["refunds"].rows
    assert refund["amount"] == Decimal("8.67") and refund["status"] == "succeeded" and refund["reason"] == "customer"
    [detail] = t["refund_details"].rows
    assert detail["order_detail_id"] == make_id("od", "shopify", "gid://shopify/LineItem/1399000000002")
    assert detail["restock"] is True
    assert changes.recompute_refund_status == [order_id]


def test_product_mapping():
    t = by_table(map_product(fixture_json("products_create.json"), MemoryCoreWriter()))
    [item] = t["items"].rows
    assert item["category"] == "Hair Care" and item["brand"] == "Luma Botanics"
    assert item["description"].startswith("A lightweight finishing oil")
    assert [v["sku"] for v in t["item_variants"].rows] == ["HC-9001-1", "HC-9001-2"]
    assert all(v["cost"] is None for v in t["item_variants"].rows)  # COALESCE keeps stored cost


def test_customer_mapping():
    t = by_table(map_customer(fixture_json("customers_create.json"), MemoryCoreWriter()))
    [c] = t["customers"].rows
    assert c["segment"] == "SMB" and c["region"] == "West" and c["marketing_opt_in"] is True
    assert c["status"] == "active" and c["country"] == "US"
    assert t["addresses"].rows[0]["line1"] == "1420 NW Lovejoy St Apt 5"


def test_merge_sql_is_parameterized():
    t = by_table(map_customer(fixture_json("customers_create.json"), MemoryCoreWriter()))
    t["customers"].rows[0]["last_name"] = "O'Brien'); DROP TABLE x; --"
    sql, params = build_merge("`c`.`core`.`customers`", "customers", t["customers"])
    assert "DROP TABLE" not in sql
    assert "O'Brien" in params.values().__repr__()
    assert "WHEN MATCHED AND (t.`updated_at` IS NULL" in sql
    assert "t.`created_at` =" not in sql  # keep_on_update
    assert "COALESCE(s.`segment`, CAST(:d_segment AS STRING))" in sql


def test_merge_dedupes_source_keys():
    from app.services.shopify_mapper import Upsert

    u = Upsert("payments", "payment_id", [{"payment_id": "p1", "amount": Decimal("1")}, {"payment_id": "p1", "amount": Decimal("2")}])
    sql, params = build_merge("t", "payments", u)
    assert sql.count("SELECT CAST") == 1
    assert params["r0_1"] == Decimal("2")


def test_merge_rejects_unknown_columns():
    from app.services.shopify_mapper import Upsert

    with pytest.raises(ValueError):
        build_merge("t", "payments", Upsert("payments", "payment_id", [{"payment_id": "p", "evil`col": 1}]))
