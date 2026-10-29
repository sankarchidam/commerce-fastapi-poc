"""Build and publish the "Commerce KPIs" AI/BI (Lakeview) dashboard over the metric views.

    python -m scripts.publish_dashboard            # create or update the draft, then publish it
    python -m scripts.publish_dashboard --check    # only run every dataset query with default parameters
    python -m scripts.publish_dashboard --out d.lvdash.json   # also write the JSON (importable in the UI)

The dashboard is defined here, in code, so it lives in the repo next to the metric views it
reads. Every dataset is a SQL query over `{metrics}.*` that wraps measures in MEASURE().
Filters are dashboard *parameters* (date_from, date_to, region) used inside those queries,
so ratios such as AOV and refund rate are computed by the metric view for the filtered slice
rather than averaged by the chart.

Re-running updates the same dashboard: it is found by display name under --parent-path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import Settings, get_settings
from app.db import Database, render

DISPLAY_NAME = "Commerce KPIs (metric views)"
DEFAULT_FROM = date(2025, 1, 1)
DEFAULT_TO = date(2026, 12, 31)
ALL = "All"

ORDER_FILTER = "order_date BETWEEN :date_from AND :date_to AND (:region = 'All' OR region = :region)"

USD = {"type": "number-currency", "currencyCode": "USD", "abbreviation": "compact",
       "decimalPlaces": {"type": "max", "places": 1}}
USD_EXACT = {"type": "number-currency", "currencyCode": "USD", "abbreviation": "none",
             "decimalPlaces": {"type": "exact", "places": 2}}
PCT = {"type": "number-percent", "decimalPlaces": {"type": "max", "places": 1}}
AXIS = {
    "gmv_usd": "GMV (USD)", "net_revenue_usd": "Net revenue (USD)", "valid_orders": "Orders", "aov_usd": "AOV (USD)",
    "refund_rate": "Refund rate", "line_revenue_usd": "Revenue (USD)", "refunded_usd": "Refunded (USD)",
    "buyers": "Buyers", "avg_ltv_usd": "Avg LTV (USD)",
}
COUNT = {"type": "number-plain", "abbreviation": "compact", "decimalPlaces": {"type": "max", "places": 1}}


def _id(*parts: str) -> str:
    # Stable 8-hex ids, so re-publishing keeps widget identity (and any viewer state).
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


@dataclass
class Dataset:
    key: str
    title: str
    sql: str
    params: list[str] = field(default_factory=lambda: ["date_from", "date_to", "region"])

    @property
    def name(self) -> str:
        return _id("dataset", self.key)


def datasets() -> dict[str, Dataset]:
    ds = [
        Dataset("kpis", "Order KPIs", f"""
SELECT
  MEASURE(gmv_usd)         AS gmv_usd,
  MEASURE(net_revenue_usd) AS net_revenue_usd,
  MEASURE(valid_orders)    AS valid_orders,
  MEASURE(aov_usd)         AS aov_usd,
  MEASURE(refund_rate)     AS refund_rate,
  MEASURE(buyers)          AS buyers
FROM {{metrics}}.order_metrics
WHERE {ORDER_FILTER}"""),
        Dataset("monthly", "Orders by month", f"""
SELECT
  order_month,
  MEASURE(gmv_usd)         AS gmv_usd,
  MEASURE(net_revenue_usd) AS net_revenue_usd,
  MEASURE(valid_orders)    AS valid_orders,
  MEASURE(aov_usd)         AS aov_usd,
  MEASURE(refund_rate)     AS refund_rate
FROM {{metrics}}.order_metrics
WHERE {ORDER_FILTER}
GROUP BY ALL
ORDER BY order_month"""),
        Dataset("segments", "Orders by segment", f"""
SELECT segment, MEASURE(gmv_usd) AS gmv_usd, MEASURE(aov_usd) AS aov_usd
FROM {{metrics}}.order_metrics
WHERE {ORDER_FILTER}
GROUP BY ALL"""),
        Dataset("channels", "Orders by channel", f"""
SELECT channel, MEASURE(gmv_usd) AS gmv_usd, MEASURE(valid_orders) AS valid_orders
FROM {{metrics}}.order_metrics
WHERE {ORDER_FILTER}
GROUP BY ALL"""),
        Dataset("categories", "Sales by category", f"""
SELECT
  category,
  MEASURE(line_revenue_usd) AS line_revenue_usd,
  MEASURE(units_sold)       AS units_sold,
  MEASURE(discount_rate)    AS discount_rate
FROM {{metrics}}.sales_line_metrics
WHERE {ORDER_FILTER}
GROUP BY ALL"""),
        Dataset("products", "Top products", f"""
SELECT
  item_name,
  category,
  MEASURE(units_sold)         AS units_sold,
  MEASURE(line_revenue_usd)   AS line_revenue_usd,
  MEASURE(avg_unit_price_usd) AS avg_unit_price_usd
FROM {{metrics}}.sales_line_metrics
WHERE {ORDER_FILTER}
GROUP BY ALL
ORDER BY line_revenue_usd DESC
LIMIT 15"""),
        Dataset("refunds_monthly", "Refunds by month", """
SELECT refund_month, MEASURE(refunds) AS refunds, MEASURE(refunded_usd) AS refunded_usd
FROM {metrics}.refund_metrics
WHERE refund_date BETWEEN :date_from AND :date_to AND (:region = 'All' OR region = :region)
GROUP BY ALL
ORDER BY refund_month"""),
        Dataset("refund_reasons", "Refunds by reason", """
SELECT reason, MEASURE(refunds) AS refunds, MEASURE(refunded_usd) AS refunded_usd
FROM {metrics}.refund_metrics
WHERE refund_date BETWEEN :date_from AND :date_to AND (:region = 'All' OR region = :region)
GROUP BY ALL"""),
        Dataset("cohorts", "Customer cohorts", """
SELECT
  cohort_month,
  MEASURE(buyers)      AS buyers,
  MEASURE(repeat_rate) AS repeat_rate,
  MEASURE(avg_ltv_usd) AS avg_ltv_usd
FROM {metrics}.customer_metrics
WHERE first_order_date BETWEEN :date_from AND :date_to AND (:region = 'All' OR region = :region)
GROUP BY ALL
ORDER BY cohort_month"""),
        Dataset("regions", "Regions (filter values)", """
SELECT 'All' AS region
UNION ALL
SELECT DISTINCT region FROM {core}.customers WHERE region IS NOT NULL""", params=[]),
    ]
    return {d.key: d for d in ds}


# ---------------------------------------------------------------- widgets
def _pos(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "width": w, "height": h}


def _query(ds: Dataset, fields: list[str]) -> list[dict[str, Any]]:
    return [{
        "name": "main_query",
        "query": {
            "datasetName": ds.name,
            "fields": [{"name": f, "expression": f"`{f}`"} for f in fields],
            "disaggregated": True,  # rows are already aggregated by the metric view
        },
    }]


def _frame(title: str, description: str | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"showTitle": True, "title": title}
    if description:
        frame["showDescription"] = True
        frame["description"] = description
    return frame


def counter(page: str, ds: Dataset, fld: str, title: str, fmt: dict, pos: dict) -> dict[str, Any]:
    return {"widget": {
        "name": _id(page, "counter", fld),
        "queries": _query(ds, [fld]),
        "spec": {"version": 2, "widgetType": "counter",
                 "encodings": {"value": {"fieldName": fld, "displayName": title, "format": fmt}},
                 "frame": _frame(title)},
    }, "position": pos}


def chart(page: str, kind: str, ds: Dataset, x: str, y: str, title: str, fmt: dict, pos: dict, *,
          x_type: str = "categorical", y_title: str | None = None, description: str | None = None) -> dict[str, Any]:
    x_enc: dict[str, Any] = {"fieldName": x, "scale": {"type": x_type}, "displayName": x.replace("_", " ")}
    if x_type == "categorical":
        x_enc["scale"]["sort"] = {"by": "y-reversed"}
    return {"widget": {
        "name": _id(page, kind, ds.key, y),
        "queries": _query(ds, [x, y]),
        "spec": {"version": 3, "widgetType": kind,
                 "encodings": {
                     "x": x_enc,
                     "y": {"fieldName": y, "scale": {"type": "quantitative"}, "displayName": y_title or AXIS.get(y, y),
                           "format": fmt},
                 },
                 "frame": _frame(title, description)},
    }, "position": pos}


def table(page: str, ds: Dataset, columns: list[tuple[str, str, dict | None]], title: str, pos: dict) -> dict[str, Any]:
    cols = []
    for i, (fld, label, fmt) in enumerate(columns):
        col: dict[str, Any] = {"fieldName": fld, "displayName": label, "order": i}
        if fmt:
            col["numberFormat"] = fmt
        cols.append(col)
    return {"widget": {
        "name": _id(page, "table", ds.key),
        "queries": _query(ds, [c[0] for c in columns]),
        "spec": {"version": 2, "widgetType": "table", "encodings": {"columns": cols}, "frame": _frame(title)},
    }, "position": pos}


def param_filter(page: str, kind: str, param: str, title: str, targets: list[Dataset], pos: dict,
                 values_from: Dataset | None = None) -> dict[str, Any]:
    """A filter widget bound to the `param` parameter of every target dataset on the page."""
    queries, fields = [], []
    for ds in targets:
        qname = f"param_{ds.key}_{param}"
        queries.append({"name": qname, "query": {
            "datasetName": ds.name, "parameters": [{"name": param, "keyword": param}], "disaggregated": False}})
        fields.append({"parameterName": param, "queryName": qname})
    if values_from is not None:
        queries.append({"name": f"values_{param}", "query": {
            "datasetName": values_from.name,
            "fields": [{"name": param, "expression": f"`{param}`"}],
            "disaggregated": False}})
        fields.append({"fieldName": param, "displayName": param, "queryName": f"values_{param}"})
    return {"widget": {
        "name": _id(page, "filter", param),
        "queries": queries,
        "spec": {"version": 2, "widgetType": kind, "encodings": {"fields": fields}, "frame": _frame(title)},
    }, "position": pos}


def text(page: str, key: str, lines: list[str], pos: dict) -> dict[str, Any]:
    # Lines are concatenated as-is, so each one needs its own trailing newline.
    lines = [ln + "\n" for ln in lines]
    return {"widget": {"name": _id(page, "text", key), "multilineTextboxSpec": {"lines": lines}}, "position": pos}


def filters(page: str, targets: list[Dataset], regions: Dataset, y: int, date_label: str) -> list[dict[str, Any]]:
    return [
        param_filter(page, "filter-date-picker", "date_from", f"{date_label} from", targets, _pos(0, y, 2, 2)),
        param_filter(page, "filter-date-picker", "date_to", f"{date_label} to", targets, _pos(2, y, 2, 2)),
        param_filter(page, "filter-single-select", "region", "Region", targets, _pos(4, y, 2, 2), values_from=regions),
    ]


def build(settings: Settings) -> dict[str, Any]:
    d = datasets()
    sales_ds = [d[k] for k in ("kpis", "monthly", "segments", "channels", "categories", "products")]
    after_ds = [d[k] for k in ("refunds_monthly", "refund_reasons", "cohorts")]

    sales = "sales"
    sales_layout = [
        text(sales, "title", [
            "## Commerce KPIs",
            "",
            f"Live from Unity Catalog metric views in `{settings.databricks_catalog}.{settings.databricks_schema_metrics}`. "
            "Revenue excludes cancelled orders; USD values convert through `core.fx_rates`.",
        ], _pos(0, 0, 6, 2)),
        *filters(sales, sales_ds, d["regions"], 2, "Order date"),
        counter(sales, d["kpis"], "gmv_usd", "GMV", USD, _pos(0, 4, 1, 3)),
        counter(sales, d["kpis"], "net_revenue_usd", "Net revenue", USD, _pos(1, 4, 1, 3)),
        counter(sales, d["kpis"], "valid_orders", "Orders", COUNT, _pos(2, 4, 1, 3)),
        counter(sales, d["kpis"], "aov_usd", "AOV", USD_EXACT, _pos(3, 4, 1, 3)),
        counter(sales, d["kpis"], "buyers", "Buyers", COUNT, _pos(4, 4, 1, 3)),
        counter(sales, d["kpis"], "refund_rate", "Refund rate", PCT, _pos(5, 4, 1, 3)),
        chart(sales, "line", d["monthly"], "order_month", "gmv_usd", "GMV by month", USD, _pos(0, 7, 3, 6),
              x_type="temporal"),
        chart(sales, "line", d["monthly"], "order_month", "net_revenue_usd", "Net revenue by month", USD,
              _pos(3, 7, 3, 6), x_type="temporal", description="GMV minus refunds, refunds attributed to the order month"),
        chart(sales, "bar", d["monthly"], "order_month", "valid_orders", "Orders by month", COUNT, _pos(0, 13, 2, 6),
              x_type="temporal"),
        chart(sales, "line", d["monthly"], "order_month", "aov_usd", "AOV by month", USD_EXACT, _pos(2, 13, 2, 6),
              x_type="temporal"),
        chart(sales, "line", d["monthly"], "order_month", "refund_rate", "Refund rate by month", PCT,
              _pos(4, 13, 2, 6), x_type="temporal"),
        chart(sales, "bar", d["categories"], "category", "line_revenue_usd", "Revenue by category", USD,
              _pos(0, 19, 2, 6)),
        chart(sales, "bar", d["segments"], "segment", "gmv_usd", "GMV by segment", USD, _pos(2, 19, 2, 6)),
        chart(sales, "bar", d["channels"], "channel", "gmv_usd", "GMV by channel", USD, _pos(4, 19, 2, 6)),
        table(sales, d["products"], [
            ("item_name", "Product", None), ("category", "Category", None), ("units_sold", "Units", None),
            ("line_revenue_usd", "Revenue (USD)", USD_EXACT), ("avg_unit_price_usd", "Avg price (USD)", USD_EXACT),
        ], "Top 15 products by revenue", _pos(0, 25, 6, 8)),
    ]

    after = "after_sale"
    after_layout = [
        text(after, "title", [
            "## Refunds & customers",
            "",
            "Refunds are dated by refund date here (the Sales page attributes them to the order month). "
            "Cohorts are customers grouped by the month of their first non-cancelled order.",
        ], _pos(0, 0, 6, 2)),
        *filters(after, after_ds, d["regions"], 2, "Refund / first-order date"),
        chart(after, "bar", d["refunds_monthly"], "refund_month", "refunded_usd", "Refunded by month", USD,
              _pos(0, 4, 3, 6), x_type="temporal"),
        chart(after, "bar", d["refund_reasons"], "reason", "refunded_usd", "Refunded by reason", USD,
              _pos(3, 4, 3, 6)),
        chart(after, "bar", d["cohorts"], "cohort_month", "buyers", "New buyers by cohort month", COUNT,
              _pos(0, 10, 3, 6), x_type="temporal"),
        chart(after, "line", d["cohorts"], "cohort_month", "avg_ltv_usd", "Avg LTV by cohort month", USD_EXACT,
              _pos(3, 10, 3, 6), x_type="temporal", description="Net lifetime value per buyer, USD"),
        table(after, d["cohorts"], [
            ("cohort_month", "Cohort", None), ("buyers", "Buyers", None),
            ("repeat_rate", "Repeat rate", PCT), ("avg_ltv_usd", "Avg LTV (USD)", USD_EXACT),
        ], "Cohorts", _pos(0, 16, 6, 8)),
    ]

    def dataset_json(ds: Dataset) -> dict[str, Any]:
        out: dict[str, Any] = {"name": ds.name, "displayName": ds.title,
                               "queryLines": [ln + "\n" for ln in render(ds.sql.strip(), settings).splitlines()]}
        defaults = {"date_from": ("DATE", DEFAULT_FROM.isoformat()), "date_to": ("DATE", DEFAULT_TO.isoformat()),
                    "region": ("STRING", ALL)}
        if ds.params:
            out["parameters"] = [{
                "displayName": p, "keyword": p, "dataType": defaults[p][0],
                "defaultSelection": {"values": {"dataType": defaults[p][0], "values": [{"value": defaults[p][1]}]}},
            } for p in ds.params]
        return out

    return {
        "datasets": [dataset_json(ds) for ds in d.values()],
        "pages": [
            {"name": _id("page", sales), "displayName": "Sales", "pageType": "PAGE_TYPE_CANVAS", "layout": sales_layout},
            {"name": _id("page", after), "displayName": "Refunds & customers", "pageType": "PAGE_TYPE_CANVAS",
             "layout": after_layout},
        ],
    }


# ---------------------------------------------------------------- Databricks
def check(settings: Settings) -> bool:
    """Run every dataset query with its default parameters; the dashboard can't show what fails here."""
    db = Database(settings)
    ok = True
    params = {"date_from": DEFAULT_FROM, "date_to": DEFAULT_TO, "region": ALL}
    with db.session() as s:
        for ds in datasets().values():
            t0 = time.perf_counter()
            try:
                rows = s.fetch_all(render(ds.sql, settings), {p: params[p] for p in ds.params})
                print(f"  ok  {time.perf_counter() - t0:4.1f}s {ds.key:<16} {len(rows):>4} rows")
            except Exception as exc:
                ok = False
                print(f"  BAD {ds.key:<16} {str(exc)[:300]}")
    db.close()
    return ok


class Api:
    def __init__(self, settings: Settings) -> None:
        self.base = f"https://{settings.databricks_host}"
        self.token = settings.databricks_token

    def call(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:1000]}")
        return json.loads(raw) if raw else {}


def find_existing(api: Api, parent_path: str) -> str | None:
    token = None
    while True:
        page = api.call("GET", "/api/2.0/lakeview/dashboards?page_size=100" + (f"&page_token={token}" if token else ""))
        for dash in page.get("dashboards", []):
            if dash.get("display_name") != DISPLAY_NAME:
                continue
            # The list response has no path, so fetch the dashboard to check its folder.
            path = api.call("GET", f"/api/2.0/lakeview/dashboards/{dash['dashboard_id']}").get("path", "")
            if path.startswith(parent_path.rstrip("/") + "/"):
                return dash["dashboard_id"]
        token = page.get("next_page_token")
        if not token:
            return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only run the dataset queries")
    parser.add_argument("--out", help="also write the dashboard JSON to this file")
    parser.add_argument("--parent-path", help="workspace folder (default: your home folder)")
    parser.add_argument("--no-publish", action="store_true", help="update the draft only")
    parser.add_argument("--embed-credentials", action="store_true",
                        help="published viewers query with your credentials instead of their own")
    args = parser.parse_args()

    settings = get_settings()
    print("checking dataset queries")
    if not check(settings):
        sys.exit("a dataset query failed; fix it before publishing")
    if args.check:
        return

    dashboard = build(settings)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(dashboard, fh, indent=2)
        print(f"wrote {args.out}")

    api = Api(settings)
    parent = args.parent_path or f"/Users/{api.call('GET', '/api/2.0/preview/scim/v2/Me')['userName']}"
    warehouse_id = settings.databricks_http_path.rstrip("/").rsplit("/", 1)[-1]
    body = {"display_name": DISPLAY_NAME, "warehouse_id": warehouse_id, "parent_path": parent,
            "serialized_dashboard": json.dumps(dashboard)}
    existing = find_existing(api, parent)
    if existing:
        result = api.call("PATCH", f"/api/2.0/lakeview/dashboards/{existing}", body)
        print(f"updated draft {existing}")
    else:
        result = api.call("POST", "/api/2.0/lakeview/dashboards", body)
        print(f"created draft {result['dashboard_id']}")
    dashboard_id = result["dashboard_id"]
    if not args.no_publish:
        api.call("POST", f"/api/2.0/lakeview/dashboards/{dashboard_id}/published",
                 {"warehouse_id": warehouse_id, "embed_credentials": args.embed_credentials})
        print("published")
    print(f"\n{api.base}/dashboardsv3/{dashboard_id}/published" if not args.no_publish
          else f"\n{api.base}/dashboardsv3/{dashboard_id}")


if __name__ == "__main__":
    main()
