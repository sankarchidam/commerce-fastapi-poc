from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
import yaml

from app.config import Settings
from app.metrics import MetricViewError, build_query, load_views, parse_view, render_create
from tests.conftest import API_KEY

AUTH = {"X-API-Key": API_KEY}
SETTINGS = Settings(databricks_catalog="cat", databricks_schema_core="c", databricks_schema_metrics="m")


def test_every_view_parses_and_is_well_formed():
    views = load_views()
    assert set(views) == {"order_metrics", "sales_line_metrics", "refund_metrics", "customer_metrics"}
    for v in views.values():
        assert v.spec["version"] == "1.1"
        assert v.time_dimension in v.dimensions
        assert "api" not in v.spec
        names = set(v.dimensions) | set(v.measures)
        for m in v.spec["measures"]:
            # Composed measures may only reference measures of the same view.
            for ref in re.findall(r"MEASURE\((\w+)\)", m["expr"]):
                assert ref in v.measures, f"{v.name}.{m['name']} references unknown measure {ref}"
        assert len(names) == len(v.dimensions) + len(v.measures)


def test_render_create_substitutes_schemas_and_strips_api():
    sql = render_create(load_views()["order_metrics"], SETTINGS)
    assert sql.startswith("CREATE OR REPLACE VIEW `cat`.`m`.`order_metrics`\nWITH METRICS\nLANGUAGE YAML\nAS $$\n")
    body = yaml.safe_load(sql.split("$$")[1])
    assert body["source"] == "`cat`.`c`.v_order_summary"
    assert {j["source"] for j in body["joins"]} == {"`cat`.`c`.customers", "`cat`.`c`.fx_rates"}
    assert "api" not in body and "{core}" not in sql


def test_join_on_key_is_not_a_boolean():
    # YAML 1.1 reads a bare `on:` as True; Databricks then rejects the join.
    for v in load_views().values():
        for join in v.spec.get("joins", []):
            assert "on" in join and True not in join, v.name
    body = render_create(load_views()["order_metrics"], SETTINGS).split("$$")[1]
    assert "'on': source.customer_id = customer.customer_id" in body


def test_nested_joins_are_rendered():
    body = yaml.safe_load(render_create(load_views()["sales_line_metrics"], SETTINGS).split("$$")[1])
    ord_join = next(j for j in body["joins"] if j["name"] == "ord")
    assert {j["source"] for j in ord_join["joins"]} == {"`cat`.`c`.customers", "`cat`.`c`.fx_rates"}


def test_build_query_binds_values_and_quotes_identifiers():
    view = load_views()["order_metrics"]
    sql, params = build_query(
        view, SETTINGS, measures=["gmv_usd", "valid_orders"], dimensions=["order_month", "region"],
        filters={"region": ["West", "x' OR 1=1 --"], "channel": ["online"]},
        date_from=date(2026, 1, 1), date_to=date(2026, 3, 31), order_by="gmv_usd", descending=True, limit=11,
    )
    assert "x' OR" not in sql
    assert params == {"date_from": date(2026, 1, 1), "date_to": date(2026, 3, 31),
                      "f2": "West", "f3": "x' OR 1=1 --", "f4": "online"}
    assert "SELECT `order_month`, `region`, MEASURE(`gmv_usd`) AS `gmv_usd`, MEASURE(`valid_orders`) AS `valid_orders`" in sql
    assert "FROM `cat`.`m`.`order_metrics`" in sql
    assert "WHERE `order_date` >= :date_from AND `order_date` <= :date_to AND `region` IN (:f2, :f3) AND `channel` IN (:f4)" in sql
    assert sql.endswith("GROUP BY ALL\nORDER BY `gmv_usd` DESC NULLS LAST, `order_month`, `region`\nLIMIT 11")


def test_build_query_without_dimensions_has_no_group_by():
    sql, params = build_query(load_views()["customer_metrics"], SETTINGS, measures=["repeat_rate"])
    assert "GROUP BY" not in sql and "ORDER BY" not in sql and params == {}


@pytest.mark.parametrize("kwargs", [
    {"measures": []},
    {"measures": ["nope"]},
    {"measures": ["gmv_usd"], "dimensions": ["gmv_usd"]},           # a measure is not a dimension
    {"measures": ["gmv_usd"], "dimensions": ["region`; DROP"]},
    {"measures": ["gmv_usd", "gmv_usd"]},
    {"measures": ["gmv_usd"], "filters": {"nope": ["x"]}},
    {"measures": ["gmv_usd"], "order_by": "region"},                # must be selected
])
def test_build_query_rejects_unknown_names(kwargs):
    with pytest.raises(MetricViewError):
        build_query(load_views()["order_metrics"], SETTINGS, **kwargs)


def test_parse_view_validation():
    base = "version: '1.1'\nsource: t\ndimensions:\n  - {name: d, expr: x}\nmeasures:\n  - {name: m, expr: COUNT(1)}\n"
    assert parse_view("ok", base + "api: {time_dimension: d}\n").time_dimension == "d"
    with pytest.raises(MetricViewError):
        parse_view("bad", base)  # no time dimension
    with pytest.raises(MetricViewError):
        parse_view("bad", base.replace("name: m", "name: d") + "api: {time_dimension: d}\n")
    with pytest.raises(MetricViewError):
        parse_view("Bad-Name", base + "api: {time_dimension: d}\n")


def test_list_metric_views_endpoint(make_client):
    client = make_client()
    assert client.get("/metrics").status_code == 401
    body = client.get("/metrics", headers=AUTH).json()
    by_name = {v["name"]: v for v in body}
    om = by_name["order_metrics"]
    assert om["time_dimension"] == "order_date"
    assert om["table"].endswith(".order_metrics")
    assert {"name": "gmv_usd", "comment": "Order totals converted to USD"} in om["measures"]


def test_query_endpoint(make_client):
    rows = [{"order_month": date(2026, 9, 1), "gmv_usd": Decimal("27845.123"), "valid_orders": 276},
            {"order_month": date(2026, 10, 1), "gmv_usd": Decimal("30000"), "valid_orders": 300}]
    client = make_client({"metric_order_metrics": lambda p: rows})
    resp = client.get("/metrics/order_metrics", headers=AUTH, params={
        "measures": "gmv_usd,valid_orders", "dimensions": "order_month",
        "filter": ["region:West", "region:East"], "date_from": "2026-09-01", "limit": 1,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == [{"order_month": "2026-09-01", "gmv_usd": 27845.123, "valid_orders": 276}]
    assert body["row_count"] == 1 and body["truncated"] is True
    _, params, sql = client.app.state.fake_session.calls[0]
    assert params["date_from"] == date(2026, 9, 1)
    assert sorted(v for k, v in params.items() if k.startswith("f")) == ["East", "West"]
    assert sql.endswith("LIMIT 2")  # limit + 1 to detect truncation


def test_query_endpoint_errors(make_client):
    client = make_client()
    assert client.get("/metrics/nope?measures=x", headers=AUTH).status_code == 404
    assert client.get("/metrics/order_metrics", headers=AUTH).status_code == 422          # measures required
    assert client.get("/metrics/order_metrics?measures=bogus", headers=AUTH).status_code == 422
    assert client.get("/metrics/order_metrics?measures=gmv&filter=region", headers=AUTH).status_code == 422
    assert client.get("/metrics/order_metrics?measures=gmv&limit=1001", headers=AUTH).status_code == 422
    assert client.app.state.fake_session.calls == []
