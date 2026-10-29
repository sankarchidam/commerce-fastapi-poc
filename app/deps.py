"""FastAPI dependencies: settings, database, API-key auth, pagination helpers."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, status

from app.config import Settings, get_settings
from app.db import Database, Session

MAX_LIMIT = 200


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_session(db: Annotated[Database, Depends(get_db)]) -> Iterator[Session]:
    with db.session() as s:
        yield s


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(description="Must equal API_KEY")] = None,
) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size (max 200)")] = 50,
        offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


def paginate(
    s: Session,
    db: Database,
    query_name: str,
    params: dict[str, Any],
    order_by: str,
    page: Pagination,
) -> tuple[list[dict[str, Any]], int]:
    """Run a named base query twice: once for COUNT(*), once for the requested page.

    `order_by` is always a constant from router code. limit/offset are validated ints.
    """
    base = db.query(query_name)
    total_row = s.fetch_one(f"SELECT COUNT(*) AS total FROM (\n{base}\n) q", params)
    total = int(total_row["total"]) if total_row else 0
    if total == 0 or page.offset >= total:
        return [], total
    rows = s.fetch_all(
        f"SELECT * FROM (\n{base}\n) q ORDER BY {order_by} LIMIT {int(page.limit)} OFFSET {int(page.offset)}",
        params,
    )
    return rows, total


def like_pattern(q: str | None) -> str | None:
    """Turn free text into a case-insensitive `LIKE` pattern with wildcards escaped."""
    if q is None or not q.strip():
        return None
    escaped = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


SessionDep = Annotated[Session, Depends(get_session)]
DbDep = Annotated[Database, Depends(get_db)]
PageDep = Annotated[Pagination, Depends()]
