from __future__ import annotations

import re

from app.config import Settings
from scripts.publish_dashboard import build

SETTINGS = Settings(databricks_catalog="cat", databricks_schema_core="c", databricks_schema_metrics="m")


def _widgets(dash):
    for page in dash["pages"]:
        for item in page["layout"]:
            yield page, item


def test_widgets_reference_real_datasets_fields_and_parameters():
    dash = build(SETTINGS)
    datasets = {d["name"]: d for d in dash["datasets"]}
    names = [d["name"] for d in dash["datasets"]] + [p["name"] for p in dash["pages"]]
    for _, item in _widgets(dash):
        w = item["widget"]
        names.append(w["name"])
        for q in w.get("queries", []):
            ds = datasets[q["query"]["datasetName"]]
            sql = "".join(ds["queryLines"])
            for f in q["query"].get("fields", []):
                assert re.search(rf"\b{f['name']}\b", sql), f"{w['name']}: {f['name']} not selected by {ds['displayName']}"
            declared = {p["keyword"] for p in ds.get("parameters", [])}
            for p in q["query"].get("parameters", []):
                assert p["keyword"] in declared
        for enc in w.get("spec", {}).get("encodings", {}).get("fields", []):
            assert enc["queryName"] in {q["name"] for q in w["queries"]}
    assert len(names) == len(set(names)), "duplicate ids"
    assert all(re.fullmatch(r"[0-9a-f]{8}", n) for n in names)


def test_every_parameterized_dataset_on_a_page_is_wired_to_its_filters():
    dash = build(SETTINGS)
    datasets = {d["name"]: d for d in dash["datasets"]}
    for page in dash["pages"]:
        used = {q["query"]["datasetName"] for _, item in _widgets({"pages": [page]})
                for q in item["widget"].get("queries", []) if "fields" in q["query"]}
        bound: dict[str, set[str]] = {}
        for _, item in _widgets({"pages": [page]}):
            for q in item["widget"].get("queries", []):
                for p in q["query"].get("parameters", []):
                    bound.setdefault(q["query"]["datasetName"], set()).add(p["keyword"])
        for name in used:
            params = {p["keyword"] for p in datasets[name].get("parameters", [])}
            assert params <= bound.get(name, set()), f"{page['displayName']}: {datasets[name]['displayName']} unfiltered"


def test_layout_fits_grid_without_overlap():
    for page in build(SETTINGS)["pages"]:
        cells = set()
        for item in page["layout"]:
            p = item["position"]
            assert p["x"] >= 0 and p["x"] + p["width"] <= 6
            for x in range(p["x"], p["x"] + p["width"]):
                for y in range(p["y"], p["y"] + p["height"]):
                    assert (x, y) not in cells, f"overlap on {page['displayName']} at {(x, y)}"
                    cells.add((x, y))


def test_queries_read_the_configured_metric_views():
    sql = "".join(ln for d in build(SETTINGS)["datasets"] for ln in d["queryLines"])
    assert "`cat`.`m`.order_metrics" in sql and "{metrics}" not in sql and "MEASURE(" in sql
