from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.deps import DbDep, require_api_key
from app.models import DatabricksHealth, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness (no auth, no Databricks call)")
def health() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/health/databricks",
    response_model=DatabricksHealth,
    dependencies=[Depends(require_api_key)],
    summary="SELECT 1 plus catalog/schema visibility",
    responses={503: {"model": DatabricksHealth}},
)
def databricks_health(db: DbDep, settings: Annotated[Settings, Depends(get_settings)]) -> DatabricksHealth | JSONResponse:
    result = DatabricksHealth(status="ok", host=settings.databricks_host, catalog=settings.databricks_catalog)
    t0 = time.perf_counter()
    try:
        with db.session() as s:
            s.fetch_one("SELECT 1 AS ok")
            result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            schemas = {
                str(next(iter(r.values())))
                for r in s.fetch_all(f"SHOW SCHEMAS IN `{settings.databricks_catalog}`")
            }
            result.core_schema_visible = settings.databricks_schema_core in schemas
            result.raw_schema_visible = settings.databricks_schema_raw in schemas
            if result.core_schema_visible:
                result.core_tables = sorted(r["tableName"] for r in s.fetch_all(f"SHOW TABLES IN {settings.core}"))
    except Exception as exc:
        result.status = "error"
        result.error = str(exc)[:500]
        return JSONResponse(result.model_dump(mode="json"), status_code=503)
    return result
