"""Test doubles: Databricks is never contacted in unit tests."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("SHOPIFY_WEBHOOK_SECRET", "test-secret")

from app.config import Settings, get_settings  # noqa: E402
from app.db import load_query  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.ingest import WebhookIngestor  # noqa: E402
from app.services.shopify_mapper import Changes  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "shopify"
API_KEY = "test-api-key"
SECRET = "test-secret"

_NAME = re.compile(r"--\s*name:\s*(\w+)")


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    return json.loads(load_fixture(name))


class FakeSession:
    """Answers named queries from canned data; records every call."""

    def __init__(self, handlers: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        match = _NAME.search(sql)
        name = match.group(1) if match else "?"
        self.calls.append((name, params, sql))
        rows = self.handlers.get(name, lambda p: [])(params)
        if sql.lstrip().startswith("SELECT COUNT(*) AS total FROM ("):
            return [{"total": len(rows)}]
        page = re.search(r"LIMIT (\d+) OFFSET (\d+)\s*$", sql)
        if page:
            limit, offset = int(page.group(1)), int(page.group(2))
            return rows[offset : offset + limit]
        return rows

    def fetch_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    execute = fetch_all


class FakeDatabase:
    def __init__(self, session: FakeSession, settings: Settings) -> None:
        self.session_obj = session
        self.settings = settings

    @contextmanager
    def session(self) -> Iterator[FakeSession]:
        yield self.session_obj

    def query(self, name: str) -> str:
        return load_query(name, self.settings)

    def close(self) -> None:
        pass


class MemoryRawStore:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def get_status(self, event_id: str) -> str | None:
        e = self.events.get(event_id)
        return e["processing_status"] if e else None

    def insert(self, event: dict[str, Any]) -> None:
        self.events.setdefault(event["event_id"], dict(event))

    def mark(self, event_id: str, status: str, error: str | None) -> None:
        self.events[event_id].update(processing_status=status, error_message=error)


class MemoryCoreWriter:
    def __init__(self) -> None:
        self.applied: list[Changes] = []
        self.customers: dict[str, str] = {}
        self.orders: set[str] = set()

    def customer_id_for(self, provider: str, provider_customer_id: str | None, email: str | None) -> str | None:
        return self.customers.get(provider_customer_id or "") or self.customers.get(email or "")

    def order_exists(self, order_id: str) -> bool:
        return order_id in self.orders

    def apply(self, changes: Changes) -> dict[str, int]:
        self.applied.append(changes)
        out: dict[str, int] = {}
        for u in changes.upserts:
            out[u.table] = out.get(u.table, 0) + len(u.rows)
            if u.table == "orders":
                self.orders.update(r["order_id"] for r in u.rows)
        return out


class MemoryIngestBackend:
    def __init__(self) -> None:
        self.raw = MemoryRawStore()
        self.core = MemoryCoreWriter()

    @contextmanager
    def open(self) -> Iterator[tuple[MemoryRawStore, MemoryCoreWriter]]:
        yield self.raw, self.core


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def backend() -> MemoryIngestBackend:
    return MemoryIngestBackend()


@pytest.fixture
def make_client(settings: Settings, backend: MemoryIngestBackend) -> Callable[..., TestClient]:
    def _make(handlers: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] | None = None) -> TestClient:
        app = create_app()
        session = FakeSession(handlers or {})
        app.state.db = FakeDatabase(session, settings)
        app.state.ingestor = WebhookIngestor(backend, settings)
        app.state.fake_session = session
        return TestClient(app)

    return _make
