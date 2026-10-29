"""Unity Catalog metric views: registry, DDL rendering and safe query building.

Each `sql/metric_views/<name>.yaml` is one metric view, created as
`{catalog}.{metrics}.<name>` by `scripts/bootstrap_databricks.py`. The YAML is the
Databricks metric-view spec plus one extra top-level block, `api:`, which this module
strips before sending it to Databricks:

    api:
      time_dimension: order_date   # DATE dimension that date_from / date_to filter on

The registry built from the same files is what `GET /metrics/{view}` validates against.
Measure and dimension names are SQL identifiers and cannot be bound as parameters, so a
request may only use names that appear in the YAML. Filter values are always bound.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.db import render

METRIC_VIEW_DIR = Path(__file__).resolve().parent.parent / "sql" / "metric_views"
MAX_FIELDS = 20

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class _Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans: only true/false.

    PyYAML follows YAML 1.1, where a bare `on:` key (every join has one) parses as True.
    """


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class MetricViewError(ValueError):
    """A metric view definition or a metric query is invalid."""


@dataclass(frozen=True)
class MetricField:
    name: str
    comment: str | None = None


@dataclass(frozen=True)
class MetricView:
    name: str
    comment: str | None
    time_dimension: str
    dimensions: dict[str, MetricField]
    measures: dict[str, MetricField]
    spec: dict[str, Any] = field(repr=False)  # the Databricks YAML, without `api:`


def _fields(view: str, kind: str, entries: list[dict[str, Any]]) -> dict[str, MetricField]:
    out: dict[str, MetricField] = {}
    for entry in entries or []:
        name = entry.get("name", "")
        if not _NAME.match(name):
            raise MetricViewError(f"{view}: invalid {kind} name {name!r}")
        if name in out:
            raise MetricViewError(f"{view}: duplicate {kind} {name!r}")
        if not entry.get("expr"):
            raise MetricViewError(f"{view}: {kind} {name!r} has no expr")
        out[name] = MetricField(name, entry.get("comment"))
    return out


def parse_view(name: str, text: str) -> MetricView:
    if not _NAME.match(name):
        raise MetricViewError(f"invalid metric view name {name!r}")
    spec = yaml.load(text, Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    if not isinstance(spec, dict) or "source" not in spec or "version" not in spec:
        raise MetricViewError(f"{name}: needs at least `version` and `source`")
    api = spec.pop("api", None) or {}
    dimensions = _fields(name, "dimension", spec.get("dimensions"))
    measures = _fields(name, "measure", spec.get("measures"))
    if not measures:
        raise MetricViewError(f"{name}: no measures")
    if clash := dimensions.keys() & measures.keys():
        raise MetricViewError(f"{name}: names used as both dimension and measure: {sorted(clash)}")
    time_dimension = api.get("time_dimension")
    if time_dimension not in dimensions:
        raise MetricViewError(f"{name}: api.time_dimension {time_dimension!r} is not a dimension")
    return MetricView(name, spec.get("comment"), time_dimension, dimensions, measures, spec)


@lru_cache
def load_views(directory: Path = METRIC_VIEW_DIR) -> dict[str, MetricView]:
    views = {p.stem: parse_view(p.stem, p.read_text()) for p in sorted(directory.glob("*.yaml"))}
    if not views:
        raise MetricViewError(f"no metric views in {directory}")
    return views


def _substitute(value: Any, settings: Settings) -> Any:
    if isinstance(value, str):
        return render(value, settings)
    if isinstance(value, list):
        return [_substitute(v, settings) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, settings) for k, v in value.items()}
    return value


def render_create(view: MetricView, settings: Settings) -> str:
    """`CREATE OR REPLACE VIEW ... WITH METRICS` for one metric view."""
    # Re-dumping quotes every value, so backticked `catalog`.`schema` names stay valid YAML.
    body = yaml.safe_dump(_substitute(view.spec, settings), sort_keys=False, width=10_000)
    if "$$" in body:
        raise MetricViewError(f"{view.name}: definition may not contain '$$'")
    return f"CREATE OR REPLACE VIEW {settings.metrics}.`{view.name}`\nWITH METRICS\nLANGUAGE YAML\nAS $$\n{body}$$"


def _check(view: MetricView, kind: str, names: list[str], allowed: dict[str, MetricField]) -> None:
    unknown = [n for n in names if n not in allowed]
    if unknown:
        raise MetricViewError(f"unknown {kind} for {view.name}: {', '.join(unknown)}. Valid: {', '.join(allowed)}")
    if len(set(names)) != len(names):
        raise MetricViewError(f"duplicate {kind}")
    if len(names) > MAX_FIELDS:
        raise MetricViewError(f"at most {MAX_FIELDS} {kind}")


def build_query(
    view: MetricView,
    settings: Settings,
    *,
    measures: list[str],
    dimensions: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    order_by: str | None = None,
    descending: bool = False,
    limit: int = 100,
) -> tuple[str, dict[str, Any]]:
    """SQL + bound parameters for one metric-view query.

    Every identifier is checked against the registry before it is quoted into the SQL;
    every value goes through a `:param` marker.
    """
    dimensions = dimensions or []
    filters = filters or {}
    if not measures:
        raise MetricViewError("at least one measure is required")
    _check(view, "measures", measures, view.measures)
    _check(view, "dimensions", dimensions, view.dimensions)
    _check(view, "filter dimensions", list(filters), view.dimensions)
    if order_by is not None and order_by not in dimensions and order_by not in measures:
        raise MetricViewError(f"order_by must be one of the requested dimensions or measures, not {order_by!r}")

    params: dict[str, Any] = {}
    where: list[str] = []
    time = f"`{view.time_dimension}`"
    if date_from is not None:
        where.append(f"{time} >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        where.append(f"{time} <= :date_to")
        params["date_to"] = date_to
    for dim, values in filters.items():
        markers = []
        for value in values:
            key = f"f{len(params)}"
            params[key] = value
            markers.append(f":{key}")
        where.append(f"`{dim}` IN ({', '.join(markers)})")

    select = [f"`{d}`" for d in dimensions] + [f"MEASURE(`{m}`) AS `{m}`" for m in measures]
    sql = f"-- name: metric_{view.name}\nSELECT {', '.join(select)}\nFROM {settings.metrics}.`{view.name}`"
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if dimensions:
        sql += "\nGROUP BY ALL"
    order = [f"`{order_by}` {'DESC' if descending else 'ASC'} NULLS LAST"] if order_by else []
    order += [f"`{d}`" for d in dimensions if d != order_by]
    if order:
        sql += "\nORDER BY " + ", ".join(order)
    sql += f"\nLIMIT {int(limit)}"
    return sql, params
