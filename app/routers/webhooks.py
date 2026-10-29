from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.deps import DbDep, PageDep, SessionDep, paginate, require_api_key
from app.models import Page, WebhookAck, WebhookEvent, WebhookEventDetail
from app.services.ingest import WebhookIngestor
from app.services.shopify_mapper import MAPPERS

log = logging.getLogger("app.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_PATH_TOPIC = re.compile(r"^[a-z]+(_[a-z]+)+$")
_HEADER_TOPIC = re.compile(r"^[a-z_]+/[a-z_]+$")
STATUS_CODES = {"applied": 200, "accepted": 200, "duplicate": 200, "skipped": 202, "failed": 500}


def get_ingestor(request: Request) -> WebhookIngestor:
    return request.app.state.ingestor


def verify_shopify_hmac(body: bytes, header_value: str | None, secret: str) -> bool:
    if not header_value or not secret:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header_value.strip())


def resolve_topic(path_topic: str, header_topic: str | None) -> str:
    """`X-Shopify-Topic` wins; otherwise `orders_create` -> `orders/create`."""
    if header_topic:
        topic = header_topic.strip().lower()
        if not _HEADER_TOPIC.match(topic):
            raise HTTPException(400, f"invalid X-Shopify-Topic {header_topic!r}")
        return topic
    if not _PATH_TOPIC.match(path_topic):
        raise HTTPException(400, f"invalid topic {path_topic!r}; expected e.g. orders_create")
    resource, _, action = path_topic.partition("_")
    return f"{resource}/{action}"


def fallback_event_id(topic: str, body: bytes) -> str:
    # Deterministic, so a re-sent identical payload without a webhook id still dedupes.
    return "gen_" + hashlib.sha256(topic.encode() + b"\n" + body).hexdigest()[:32]


@router.post(
    "/shopify/{topic}",
    response_model=WebhookAck,
    summary="Shopify webhook ingress (HMAC-verified)",
    description=(
        "Verifies `X-Shopify-Hmac-Sha256` over the raw body, lands the event in `raw.webhook_events` "
        "(idempotent on `X-Shopify-Webhook-Id`) and applies it to `core.*` synchronously. "
        "With WEBHOOK_APPLY_MODE=background it returns 200 `accepted` once the raw event is stored and "
        "applies after the response. "
        "200 = applied/accepted/duplicate, 202 = stored but no mapper for the topic, 401 = bad HMAC, "
        "500 = stored but apply failed (Shopify will retry)."
    ),
    responses={
        202: {"model": WebhookAck, "description": "Unknown topic: stored, not applied"},
        401: {"description": "Invalid HMAC; nothing written to core"},
        500: {"model": WebhookAck, "description": "Stored, apply failed"},
    },
)
async def shopify_webhook(
    request: Request,
    background: BackgroundTasks,
    topic: Annotated[str, Path(examples=["orders_create"])],
    ingestor: Annotated[WebhookIngestor, Depends(get_ingestor)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_shopify_hmac_sha256: Annotated[str | None, Header()] = None,
    x_shopify_topic: Annotated[str | None, Header()] = None,
    x_shopify_webhook_id: Annotated[str | None, Header()] = None,
    x_shopify_shop_domain: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    body = await request.body()
    resolved = resolve_topic(topic, x_shopify_topic)

    if not verify_shopify_hmac(body, x_shopify_hmac_sha256, settings.shopify_webhook_secret):
        log.warning("webhook rejected: invalid HMAC", extra={"topic": resolved, "shop": x_shopify_shop_domain})
        try:
            await run_in_threadpool(
                ingestor.record_rejected, provider="shopify", topic=resolved, shop_domain=x_shopify_shop_domain, body=body
            )
        except Exception:
            log.exception("could not record rejected webhook")
        raise HTTPException(401, "invalid HMAC signature")

    event_id = (x_shopify_webhook_id or "").strip()[:200] or fallback_event_id(resolved, body)
    event = {"provider": "shopify", "topic": resolved, "event_id": event_id, "shop_domain": x_shopify_shop_domain, "body": body}
    if settings.webhook_apply_mode == "background" and resolved in MAPPERS:
        result = await run_in_threadpool(lambda: ingestor.land(**event))
        if result.status == "accepted":
            # Starlette runs sync background tasks in the threadpool after the response is sent.
            background.add_task(ingestor.apply, topic=resolved, event_id=event_id, body=body)
    else:
        result = await run_in_threadpool(lambda: ingestor.handle(**event))
    ack = WebhookAck(status=result.status, event_id=result.event_id, topic=result.topic, applied=result.applied, error=result.error)
    return JSONResponse(ack.model_dump(mode="json"), status_code=STATUS_CODES[result.status])


@router.get(
    "/events",
    response_model=Page[WebhookEvent],
    dependencies=[Depends(require_api_key)],
    summary="Recent raw webhook events (ops/debug)",
)
def list_events(
    s: SessionDep,
    db: DbDep,
    page: PageDep,
    topic: Annotated[str | None, Query(examples=["orders/create"])] = None,
    status: Literal["received", "applied", "duplicate", "skipped", "failed"] | None = None,
) -> Page[WebhookEvent]:
    rows, total = paginate(s, db, "webhook_events_list", {"topic": topic, "status": status}, "received_at DESC, event_id", page)
    return Page[WebhookEvent](items=[WebhookEvent.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/events/{event_id}",
    response_model=WebhookEventDetail,
    dependencies=[Depends(require_api_key)],
    summary="One raw webhook event including its payload",
)
def get_event(event_id: str, s: SessionDep, db: DbDep) -> WebhookEventDetail:
    row = s.fetch_one(db.query("webhook_events_get"), {"event_id": event_id})
    if row is None:
        raise HTTPException(404, f"event {event_id} not found")
    raw = row.pop("payload_json", None)
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw
    return WebhookEventDetail.model_validate({**row, "payload": payload})
