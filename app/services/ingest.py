"""Webhook ingestion: land raw event -> map -> MERGE into core -> record outcome.

POC shortcut: everything runs synchronously inside the webhook request. In production the
handler would stop after landing the raw event and a worker would apply it (Shopify expects
a response within 5 s and retries on failure).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from app.config import Settings
from app.db import Database, Session
from app.services.shopify_mapper import MAPPERS, Changes, MappingError, Upsert
from app.tables import CORE_TABLES, RAW_TABLES

log = logging.getLogger("app.ingest")

FINAL_STATUSES = {"applied", "skipped", "duplicate"}
MAX_REJECTED_PAYLOAD = 16_384


# --------------------------------------------------------------------------- MERGE builder
def build_merge(fq_table: str, table: str, upsert: Upsert) -> tuple[str, dict[str, Any]]:
    """Build a parameterized multi-row MERGE for one Upsert.

    Column names come from app.tables (never from the payload); every value is a bound
    parameter wrapped in CAST so NULLs and mixed rows type-check.
    """
    spec = dict(CORE_TABLES.get(table) or RAW_TABLES[table])
    rows_by_key: dict[Any, dict[str, Any]] = {}
    for row in upsert.rows:
        rows_by_key[row[upsert.key]] = row  # MERGE rejects duplicate source keys; last one wins
    rows = list(rows_by_key.values())
    present = {c for r in rows for c in r}
    unknown = present - spec.keys()
    if unknown:
        raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
    cols = [c for c in spec if c in present]

    params: dict[str, Any] = {}
    selects = []
    for i, row in enumerate(rows):
        exprs = []
        for j, col in enumerate(cols):
            name = f"r{i}_{j}"
            params[name] = row.get(col)
            exprs.append(f"CAST(:{name} AS {spec[col]}) AS `{col}`")
        selects.append("SELECT " + ", ".join(exprs))
    source = "\n  UNION ALL ".join(selects)

    insert_values = []
    for col in cols:
        if col in upsert.insert_defaults:
            params[f"d_{col}"] = upsert.insert_defaults[col]
            insert_values.append(f"COALESCE(s.`{col}`, CAST(:d_{col} AS {spec[col]}))")
        else:
            insert_values.append(f"s.`{col}`")

    sql = (
        f"MERGE INTO {fq_table} AS t\nUSING (\n  {source}\n) AS s\n"
        f"ON t.`{upsert.key}` = s.`{upsert.key}`\n"
    )
    if upsert.mode == "upsert":
        updatable = [c for c in cols if c != upsert.key and c not in upsert.keep_on_update]
        if updatable:
            cond = ""
            if upsert.guard and upsert.guard in cols:
                g = upsert.guard
                cond = f" AND (t.`{g}` IS NULL OR s.`{g}` IS NULL OR s.`{g}` >= t.`{g}`)"
            # COALESCE: a field missing from the payload never wipes a stored value.
            sets = ", ".join(f"t.`{c}` = COALESCE(s.`{c}`, t.`{c}`)" for c in updatable)
            sql += f"WHEN MATCHED{cond} THEN UPDATE SET {sets}\n"
    sql += (
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(f'`{c}`' for c in cols)})\n"
        f"VALUES ({', '.join(insert_values)})"
    )
    return sql, params


# --------------------------------------------------------------------------- storage protocols
class RawEventStore(Protocol):
    def get_status(self, event_id: str) -> str | None: ...

    def insert(self, event: dict[str, Any]) -> None: ...

    def mark(self, event_id: str, status: str, error: str | None) -> None: ...


class CoreWriter(Protocol):
    def customer_id_for(self, provider: str, provider_customer_id: str | None, email: str | None) -> str | None: ...

    def order_exists(self, order_id: str) -> bool: ...

    def apply(self, changes: Changes) -> dict[str, int]: ...


class IngestBackend(Protocol):
    def open(self) -> AbstractContextManager[tuple[RawEventStore, CoreWriter]]: ...


class DatabricksRawStore:
    def __init__(self, s: Session, settings: Settings) -> None:
        self.s, self.t = s, f"{settings.raw}.`webhook_events`"

    def get_status(self, event_id: str) -> str | None:
        row = self.s.fetch_one(f"SELECT processing_status FROM {self.t} WHERE event_id = :event_id", {"event_id": event_id})
        return row["processing_status"] if row else None

    def insert(self, event: dict[str, Any]) -> None:
        sql, params = build_merge(self.t, "webhook_events", Upsert("webhook_events", "event_id", [event], mode="insert_only"))
        self.s.execute(sql, params)

    def mark(self, event_id: str, status: str, error: str | None) -> None:
        self.s.execute(
            f"UPDATE {self.t} SET processing_status = :status, error_message = :error, "
            f"processed_at = current_timestamp() WHERE event_id = :event_id",
            {"status": status, "error": error, "event_id": event_id},
        )


class DatabricksCoreWriter:
    def __init__(self, s: Session, db: Database) -> None:
        self.s, self.db, self.settings = s, db, db.settings

    def _t(self, table: str) -> str:
        return f"{self.settings.core}.`{table}`"

    def customer_id_for(self, provider: str, provider_customer_id: str | None, email: str | None) -> str | None:
        row = self.s.fetch_one(
            f"""SELECT customer_id FROM {self._t('customers')}
                WHERE (provider = :provider AND provider_customer_id = :pid)
                   OR (:email IS NOT NULL AND lower(email) = :email)
                ORDER BY CASE WHEN provider_customer_id = :pid THEN 0 ELSE 1 END
                LIMIT 1""",
            {"provider": provider, "pid": provider_customer_id, "email": email},
        )
        return row["customer_id"] if row else None

    def order_exists(self, order_id: str) -> bool:
        return self.s.fetch_one(f"SELECT 1 AS x FROM {self._t('orders')} WHERE order_id = :id", {"id": order_id}) is not None

    def _merge(self, upsert: Upsert, s: Session) -> None:
        sql, params = build_merge(self._t(upsert.table), upsert.table, upsert)
        s.execute(sql, params)

    def _merge_on_own_connection(self, upsert: Upsert) -> None:
        with self.db.session() as s:
            self._merge(upsert, s)

    def apply(self, changes: Changes) -> dict[str, int]:
        """Run the MERGEs of one event concurrently.

        Each MERGE is ~1-2 s on a serverless warehouse (Delta commit), so running 7-8 of them
        serially blows Shopify's 5 s budget. They touch different tables (or the same table with
        disjoint keys) and FKs are not enforced, so order does not matter. Concurrency is bounded
        by the connection pool.
        """
        counts: dict[str, int] = {}
        upserts = changes.upserts
        if upserts:
            first, rest = upserts[0], upserts[1:]
            workers = max(1, min(len(rest), self.settings.db_pool_size - 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._merge_on_own_connection, u) for u in rest]
                self._merge(first, self.s)  # reuse the already checked-out connection
                for f in futures:
                    f.result()  # re-raise the first failure
        for upsert in upserts:
            counts[upsert.table] = counts.get(upsert.table, 0) + len({r[upsert.key] for r in upsert.rows})
        for order_id in changes.recompute_refund_status:
            self.s.execute(
                f"""MERGE INTO {self._t('orders')} AS t
                    USING (
                      SELECT o.order_id,
                             CASE WHEN r.refunded >= o.total - o.shipping - 0.01 THEN 'refunded'
                                  ELSE 'partially_refunded' END AS fs
                      FROM {self._t('orders')} o
                      JOIN (SELECT order_id, SUM(amount) AS refunded FROM {self._t('refunds')}
                            WHERE order_id = :order_id AND status = 'succeeded' GROUP BY order_id) r
                        ON r.order_id = o.order_id
                    ) AS s
                    ON t.order_id = s.order_id
                    WHEN MATCHED THEN UPDATE SET
                      t.financial_status = s.fs, t.status = s.fs, t.updated_at = current_timestamp()""",
                {"order_id": order_id},
            )
            counts["orders"] = counts.get("orders", 0) + 1
        return counts


class DatabricksIngestBackend:
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def open(self) -> Iterator[tuple[RawEventStore, CoreWriter]]:
        with self.db.session() as s:
            yield DatabricksRawStore(s, self.db.settings), DatabricksCoreWriter(s, self.db)


# --------------------------------------------------------------------------- ingestor
@dataclass
class IngestResult:
    status: Literal["applied", "accepted", "duplicate", "skipped", "failed"]
    event_id: str
    topic: str
    applied: dict[str, int]
    error: str | None = None


class WebhookIngestor:
    def __init__(self, backend: IngestBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    def handle(self, *, provider: str, topic: str, event_id: str, shop_domain: str | None, body: bytes) -> IngestResult:
        """Land + apply in one go (WEBHOOK_APPLY_MODE=sync)."""
        t0 = time.perf_counter()
        with self.backend.open() as (raw, core):
            landed = self._land(raw, provider=provider, topic=topic, event_id=event_id, shop_domain=shop_domain, body=body)
            result = landed or self._apply(raw, core, topic=topic, event_id=event_id, body=body)
        self._log(result, t0)
        return result

    def land(self, *, provider: str, topic: str, event_id: str, shop_domain: str | None, body: bytes) -> IngestResult:
        """Persist the raw event only (WEBHOOK_APPLY_MODE=background). Returns `accepted` if new."""
        t0 = time.perf_counter()
        with self.backend.open() as (raw, _core):
            result = self._land(raw, provider=provider, topic=topic, event_id=event_id, shop_domain=shop_domain, body=body)
        result = result or IngestResult("accepted", event_id, topic, {})
        self._log(result, t0)
        return result

    def apply(self, *, topic: str, event_id: str, body: bytes) -> IngestResult:
        """Apply an already-landed event (background task)."""
        t0 = time.perf_counter()
        with self.backend.open() as (raw, core):
            result = self._apply(raw, core, topic=topic, event_id=event_id, body=body)
        self._log(result, t0)
        return result

    @staticmethod
    def _log(result: IngestResult, t0: float) -> None:
        log.info(
            "webhook processed",
            extra={
                "topic": result.topic, "event_id": result.event_id, "outcome": result.status,
                "applied": result.applied, "error": result.error,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        )

    def _land(
        self, raw: RawEventStore, *, provider: str, topic: str, event_id: str, shop_domain: str | None, body: bytes
    ) -> IngestResult | None:
        """Returns a final result for duplicates, or None when the event still needs applying."""
        existing = raw.get_status(event_id)
        if existing in FINAL_STATUSES:
            return IngestResult("duplicate", event_id, topic, {})
        if existing is None:
            raw.insert({
                "event_id": event_id, "provider": provider, "topic": topic, "shop_domain": shop_domain,
                "hmac_valid": True, "payload_json": body.decode("utf-8", errors="replace"),
                "processing_status": "received", "error_message": None,
                "received_at": datetime.now(UTC), "processed_at": None,
            })
        # existing == "failed"/"received": a retry -> process again.
        return None

    def _apply(self, raw: RawEventStore, core: CoreWriter, *, topic: str, event_id: str, body: bytes) -> IngestResult:
        mapper = MAPPERS.get(topic)
        if mapper is None:
            raw.mark(event_id, "skipped", f"no mapper for topic {topic}")
            return IngestResult("skipped", event_id, topic, {})
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise MappingError("payload must be a JSON object")
            changes = mapper(payload, core, self.settings.default_currency)
            applied = core.apply(changes)
        except (MappingError, ValueError, KeyError, TypeError) as exc:
            raw.mark(event_id, "failed", f"{type(exc).__name__}: {exc}"[:1000])
            return IngestResult("failed", event_id, topic, {}, error=str(exc))
        except Exception as exc:  # Databricks errors: record, then surface as failed
            log.exception("webhook apply failed", extra={"topic": topic, "event_id": event_id})
            try:
                raw.mark(event_id, "failed", f"{type(exc).__name__}: {exc}"[:1000])
            except Exception:
                log.exception("could not record failure", extra={"event_id": event_id})
            return IngestResult("failed", event_id, topic, {}, error=type(exc).__name__)
        raw.mark(event_id, "applied", None)
        return IngestResult("applied", event_id, topic, applied)

    def record_rejected(self, *, provider: str, topic: str, shop_domain: str | None, body: bytes) -> str:
        """Log an HMAC failure in the raw table under a generated id.

        A generated id (not the sender's X-Shopify-Webhook-Id) so a forged request can never
        claim, and thereby block, the id of a genuine future event.
        """
        event_id = f"rejected_{uuid.uuid4().hex}"
        with self.backend.open() as (raw, _core):
            raw.insert({
                "event_id": event_id, "provider": provider, "topic": topic, "shop_domain": shop_domain,
                "hmac_valid": False, "payload_json": body[:MAX_REJECTED_PAYLOAD].decode("utf-8", errors="replace"),
                "processing_status": "failed", "error_message": "invalid HMAC signature",
                "received_at": datetime.now(UTC), "processed_at": datetime.now(UTC),
            })
        return event_id
