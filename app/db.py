"""Databricks SQL warehouse access.

A small LIFO pool of `databricks.sql` connections. Callers check a connection out with
`with db.session() as s:` and run any number of statements on it, so a request (or a
loader chunk) never opens a connection per row.

All user-supplied values go through native named parameters (`:name` markers). The only
text ever substituted into SQL is the catalog/schema identifiers from settings, which are
validated in `app.config`.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)

QUERY_DIR = Path(__file__).resolve().parent.parent / "sql" / "queries"


class DatabaseUnavailable(RuntimeError):
    """Raised when the warehouse cannot be reached or is not configured."""


def render(sql: str, settings: Settings) -> str:
    """Substitute `{core}` / `{raw}` with fully-qualified, backtick-quoted schema names."""
    return sql.replace("{core}", settings.core).replace("{raw}", settings.raw)


@lru_cache
def _read_query(name: str) -> str:
    path = QUERY_DIR / f"{name}.sql"
    if not path.is_file():
        raise KeyError(f"unknown named query: {name}")
    return path.read_text()


def load_query(name: str, settings: Settings) -> str:
    return render(_read_query(name), settings)


def _is_transport_error(exc: BaseException) -> bool:
    return type(exc).__module__.startswith("databricks") and type(exc).__name__ != "ServerOperationError"


class Session:
    """One checked-out connection. Thin wrapper so tests can fake it."""

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def fetch_all(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, parameters=dict(params) if params else None)
            if cur.description is None:
                return []
            return [row.asDict() for row in cur.fetchall()]

    def fetch_one(self, sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.fetch_all(sql, params)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pool: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=settings.db_pool_size)
        self._created = 0
        self._lock = threading.Lock()

    def _connect(self) -> Any:
        if not self.settings.databricks_configured:
            raise DatabaseUnavailable(
                "Databricks is not configured: set DATABRICKS_HOST, DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN"
            )
        from databricks import sql as dbsql

        try:
            return dbsql.connect(
                server_hostname=self.settings.databricks_host,
                http_path=self.settings.databricks_http_path,
                access_token=self.settings.databricks_token,
                session_configuration={"TIMEZONE": "UTC"},
                user_agent_entry="commerce-fastapi-poc",
            )
        except Exception as exc:  # the connector raises several unrelated exception types
            raise DatabaseUnavailable(f"cannot connect to Databricks at {self.settings.databricks_host}: {exc}") from exc

    def _checkout(self) -> Any:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            reserve = self._created < self.settings.db_pool_size
            if reserve:
                self._created += 1
        if reserve:
            # Connect outside the lock so parallel checkouts open connections concurrently.
            try:
                return self._connect()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        return self._pool.get(timeout=30)

    def _discard(self, conn: Any) -> None:
        with self._lock:
            self._created -= 1
        try:
            conn.close()
        except Exception:
            pass

    @contextmanager
    def session(self) -> Iterator[Session]:
        conn = self._checkout()
        healthy = True
        try:
            yield Session(conn)
        except Exception as exc:
            # A failed SQL statement (ServerOperationError) or an app-level exception does not
            # poison the connection; a connector transport error does.
            healthy = not _is_transport_error(exc)
            raise
        finally:
            if healthy:
                self._pool.put(conn)
            else:
                self._discard(conn)

    def query(self, name: str) -> str:
        return load_query(name, self.settings)

    def close(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                conn.close()
            except Exception:
                pass
