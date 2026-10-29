from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import Settings, get_settings
from app.deps import SessionDep, require_api_key
from app.metrics import MetricViewError, build_query, load_views
from app.models import MetricQueryResult, MetricViewInfo

router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Depends(require_api_key)])

MAX_ROWS = 1000


def _split(values: list[str] | None) -> list[str]:
    """Accept both `?measures=a,b` and `?measures=a&measures=b`."""
    return [part.strip() for v in values or [] for part in v.split(",") if part.strip()]


def _parse_filters(raw: list[str] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in raw or []:
        dim, sep, value = item.partition(":")
        if not sep or not dim.strip():
            raise HTTPException(422, f"filter must look like dimension:value, got {item!r}")
        out.setdefault(dim.strip(), []).append(value)
    return out


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


@router.get("", response_model=list[MetricViewInfo], summary="Metric views with their dimensions and measures")
def list_views(settings: Annotated[Settings, Depends(get_settings)]) -> list[MetricViewInfo]:
    return [
        MetricViewInfo(
            name=v.name,
            table=f"{settings.databricks_catalog}.{settings.databricks_schema_metrics}.{v.name}",
            comment=v.comment,
            time_dimension=v.time_dimension,
            dimensions=list(v.dimensions.values()),
            measures=list(v.measures.values()),
        )
        for v in load_views().values()
    ]


@router.get(
    "/{view}",
    response_model=MetricQueryResult,
    summary="Query a metric view: MEASURE()s grouped by dimensions",
    description=(
        "Runs `SELECT <dimensions>, MEASURE(<measures>) FROM {catalog}.metrics.<view> GROUP BY ALL`. "
        "Names must come from `GET /metrics`. `date_from` / `date_to` (inclusive) filter the view's time "
        "dimension; `filter=dimension:value` may repeat (same dimension = OR, different dimensions = AND). "
        "Measures are JSON numbers, not money strings, so they can go straight into a chart."
    ),
)
def query_view(
    view: str,
    s: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
    measures: Annotated[list[str], Query(description="Comma-separated or repeated", examples=["gmv_usd,valid_orders"])],
    dimensions: Annotated[list[str] | None, Query(description="Comma-separated or repeated", examples=["order_month"])] = None,
    filter: Annotated[list[str] | None, Query(description="dimension:value, repeatable", examples=["region:West"])] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    order_by: Annotated[str | None, Query(description="A requested dimension or measure")] = None,
    desc: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 100,
) -> MetricQueryResult:
    mv = load_views().get(view)
    if mv is None:
        raise HTTPException(404, f"unknown metric view {view!r}. Valid: {', '.join(load_views())}")
    m, d = _split(measures), _split(dimensions)
    try:
        sql, params = build_query(
            mv, settings, measures=m, dimensions=d, filters=_parse_filters(filter),
            date_from=date_from, date_to=date_to, order_by=order_by, descending=desc, limit=limit + 1,
        )
    except MetricViewError as exc:
        raise HTTPException(422, str(exc)) from exc
    rows = s.fetch_all(sql, params)
    truncated = len(rows) > limit
    rows = [{k: _json_value(v) for k, v in r.items()} for r in rows[:limit]]
    return MetricQueryResult(view=view, dimensions=d, measures=m, rows=rows, row_count=len(rows), truncated=truncated)
