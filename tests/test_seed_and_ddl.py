from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import Settings
from app.tables import CORE_TABLES, RAW_TABLES
from scripts.common import DDL_PATH, render_ddl, sql_literal
from scripts.generate_seed_data import generate

TODAY = datetime(2026, 10, 29, tzinfo=UTC)


@pytest.fixture(scope="module")
def ds():
    return generate(today=TODAY)


def test_scale_and_ratios(ds):
    t = ds.tables
    assert len(t["customers"]) >= 500 and len(t["items"]) >= 200 and len(t["orders"]) >= 5000
    assert all(1 <= n <= 3 for n in Counter(v["item_id"] for v in t["item_variants"]).values())
    assert all(1 <= n <= 5 for n in Counter(d["order_id"] for d in t["order_details"]).values())
    orders = len(t["orders"])
    captured = sum(p["status"] == "captured" for p in t["payments"])
    assert 0.87 <= captured / orders <= 0.93
    paid = [o for o in t["orders"] if o["status"] not in ("pending", "cancelled")]
    assert 0.65 <= len(t["fulfillments"]) / len(paid) <= 0.75
    assert 0.08 <= len(t["refunds"]) / orders <= 0.12
    first, last = min(o["ordered_at"] for o in t["orders"]), max(o["ordered_at"] for o in t["orders"])
    assert (TODAY - first).days <= 549 and (TODAY - last).days <= 1
    assert {i["category"] for i in t["items"]} >= {"Hair Care", "Skin Care", "Bath & Body", "Fragrance"}
    assert {c["segment"] for c in t["customers"]} == {"Consumer", "SMB", "Enterprise"}
    assert {c["region"] for c in t["customers"]} == {"West", "East", "North", "South"}


def test_referential_integrity(ds):
    t = ds.tables
    ids = {name: {r[CORE_TABLES[name][0][0]] for r in rows} for name, rows in t.items()}
    for name, rows in t.items():
        assert len(ids[name]) == len(rows), f"duplicate primary key in {name}"
    assert {a["customer_id"] for a in t["addresses"]} <= ids["customers"]
    assert {v["item_id"] for v in t["item_variants"]} <= ids["items"]
    assert {o["customer_id"] for o in t["orders"]} <= ids["customers"]
    assert {o["shipping_address_id"] for o in t["orders"]} <= ids["addresses"]
    for table in ("order_details", "payments", "fulfillments", "refunds"):
        assert {r["order_id"] for r in t[table]} <= ids["orders"], table
    assert {d["variant_id"] for d in t["order_details"]} <= ids["item_variants"]
    assert {d["item_id"] for d in t["order_details"]} <= ids["items"]
    assert {r["refund_id"] for r in t["refund_details"]} <= ids["refunds"]
    assert {r["order_detail_id"] for r in t["refund_details"]} <= ids["order_details"]
    assert {m["variant_id"] for m in t["inventory_movements"]} <= ids["item_variants"]


def test_money_is_consistent(ds):
    t = ds.tables
    lines = defaultdict(list)
    for d in t["order_details"]:
        assert d["line_total"] == d["unit_price"] * d["quantity"] - d["line_discount"] + d["line_tax"]
        lines[d["order_id"]].append(d)
    for o in t["orders"]:
        ls = lines[o["order_id"]]
        assert o["subtotal"] == sum(l["unit_price"] * l["quantity"] for l in ls)
        assert o["discount"] == sum(l["line_discount"] for l in ls)
        assert o["tax"] == sum(l["line_tax"] for l in ls)
        assert o["total"] == o["subtotal"] - o["discount"] + o["tax"] + o["shipping"]
    refund_lines = defaultdict(Decimal)
    for rd in t["refund_details"]:
        refund_lines[rd["refund_id"]] += rd["amount"]
    order_by_id = {o["order_id"]: o for o in t["orders"]}
    for r in t["refunds"]:
        assert r["amount"] == refund_lines[r["refund_id"]]
        assert r["amount"] <= order_by_id[r["order_id"]]["total"]


def test_timestamps_are_ordered(ds):
    t = ds.tables
    orders = {o["order_id"]: o for o in t["orders"]}
    for o in orders.values():
        assert o["ordered_at"] <= o["updated_at"] <= TODAY
        assert o["cancelled_at"] is None or o["ordered_at"] <= o["cancelled_at"] <= TODAY
    for f in t["fulfillments"]:
        assert orders[f["order_id"]]["ordered_at"] < f["shipped_at"] <= TODAY
        assert f["delivered_at"] is None or f["shipped_at"] < f["delivered_at"] <= TODAY
    shipped = {f["order_id"]: f["shipped_at"] for f in t["fulfillments"]}
    for r in t["refunds"]:
        assert shipped[r["order_id"]] <= r["refunded_at"] <= TODAY
    for p in t["payments"]:
        assert p["processed_at"] >= orders[p["order_id"]]["ordered_at"]


def test_inventory_ledger_matches(ds):
    ledger = defaultdict(int)
    for m in ds.tables["inventory_movements"]:
        ledger[m["variant_id"]] += m["quantity_delta"]
    for v in ds.tables["item_variants"]:
        assert ledger[v["variant_id"]] == v["inventory_quantity"] >= 0


def test_deterministic():
    a = generate(customers=50, items=30, orders=200, today=TODAY)
    b = generate(customers=50, items=30, orders=200, today=TODAY)
    assert a.tables == b.tables


def _ddl_columns() -> dict[str, list[str]]:
    text = DDL_PATH.read_text()
    out = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS commerce_poc\.\w+\.(\w+) \((.*?)\n\) USING DELTA", text, re.S):
        cols = []
        for line in m.group(2).splitlines():
            line = line.strip()
            if line and not line.startswith(("CONSTRAINT", "--")):
                cols.append(line.split()[0])
        out[m.group(1)] = cols
    return out


def test_tables_py_matches_ddl():
    ddl = _ddl_columns()
    for name, spec in {**CORE_TABLES, **RAW_TABLES}.items():
        assert ddl[name] == [c for c, _ in spec], name
    assert set(ddl) == set(CORE_TABLES) | set(RAW_TABLES)


def test_render_ddl_uses_configured_names():
    s = Settings(databricks_catalog="sandbox", databricks_schema_core="c1", databricks_schema_raw="r1")
    stmts = render_ddl(s)
    assert all("commerce_poc.core" not in st and "commerce_poc.raw" not in st for st in stmts)
    assert stmts[0] == "CREATE CATALOG IF NOT EXISTS `sandbox`"
    assert any(st.startswith("CREATE TABLE IF NOT EXISTS `sandbox`.`c1`.customers") for st in stmts)
    assert any("`sandbox`.`r1`.webhook_events" in st for st in stmts)


def test_settings_reject_bad_identifiers():
    with pytest.raises(ValueError):
        Settings(databricks_catalog="x`; DROP")


def test_sql_literal():
    assert sql_literal("O'Brien \\ x", "STRING") == "'O\\'Brien \\\\ x'"
    assert sql_literal(None, "STRING") == "NULL"
    assert sql_literal("12.30", "DECIMAL(18,2)") == "CAST(12.30 AS DECIMAL(18,2))"
    assert sql_literal(True, "BOOLEAN") == "TRUE"
    assert sql_literal(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "TIMESTAMP") == "TIMESTAMP '2026-01-02 03:04:05+00:00'"
