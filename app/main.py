"""FastAPI entrypoint: `uvicorn app.main:app --reload`."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import Database, DatabaseUnavailable
from app.logging_setup import configure_logging, request_id_var
from app.routers import customers, health, items, orders, refunds, stats, webhooks
from app.services.ingest import DatabricksIngestBackend, WebhookIngestor

log = logging.getLogger("app")

TAGS = [
    {"name": "health", "description": "Liveness and Databricks connectivity"},
    {"name": "customers", "description": "Customer profiles, addresses, lifetime value"},
    {"name": "items", "description": "Products, variants and inventory"},
    {"name": "orders", "description": "Orders with lines, payments, fulfillments and refunds"},
    {"name": "refunds", "description": "Refunds and refunded lines"},
    {"name": "webhooks", "description": "Shopify webhook ingress (HMAC) and raw event log (API key)"},
    {"name": "stats", "description": "Headline numbers"},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not hasattr(app.state, "db"):
        app.state.db = Database(settings)
    if not hasattr(app.state, "ingestor"):
        app.state.ingestor = WebhookIngestor(DatabricksIngestBackend(app.state.db), settings)
    log.info("startup", extra={"catalog": settings.databricks_catalog, "host": settings.databricks_host or None})
    yield
    if isinstance(app.state.db, Database):
        app.state.db.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Commerce POC API",
        version="0.1.0",
        description="Thin query + webhook layer over a Databricks Unity Catalog commerce lakehouse.",
        openapi_tags=TAGS,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            log.info(
                "request",
                extra={
                    "method": request.method, "path": request.url.path, "status": status,
                    "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                },
            )
            request_id_var.reset(token)

    @app.exception_handler(DatabaseUnavailable)
    async def db_unavailable(_: Request, exc: DatabaseUnavailable) -> JSONResponse:
        log.error("databricks unavailable", extra={"error": str(exc)})
        return JSONResponse({"detail": "Databricks unavailable", "error": str(exc)}, status_code=503)

    for module in (health, customers, items, orders, refunds, webhooks, stats):
        app.include_router(module.router)
    return app


app = create_app()
