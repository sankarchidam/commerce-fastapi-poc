from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import DbDep, PageDep, SessionDep, paginate, require_api_key
from app.models import Page, Refund, RefundDetail, RefundLine

router = APIRouter(prefix="/refunds", tags=["refunds"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=Page[Refund], summary="List refunds, newest first")
def list_refunds(
    s: SessionDep,
    db: DbDep,
    page: PageDep,
    order_id: str | None = None,
    status: Literal["pending", "succeeded", "failed"] | None = None,
    date_from: Annotated[date | None, Query(description="Inclusive, UTC (YYYY-MM-DD)")] = None,
    date_to: Annotated[date | None, Query(description="Inclusive, UTC (YYYY-MM-DD)")] = None,
) -> Page[Refund]:
    params = {
        "order_id": order_id,
        "status": status,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    rows, total = paginate(s, db, "refunds_list", params, "refunded_at DESC, refund_id", page)
    return Page[Refund](items=[Refund.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{refund_id}", response_model=RefundDetail, summary="Refund with refunded lines")
def get_refund(refund_id: str, s: SessionDep, db: DbDep) -> RefundDetail:
    row = s.fetch_one(db.query("refunds_get"), {"refund_id": refund_id})
    if row is None:
        raise HTTPException(404, f"refund {refund_id} not found")
    lines = [RefundLine.model_validate(r) for r in s.fetch_all(db.query("refund_lines"), {"refund_id": refund_id})]
    return RefundDetail.model_validate({**row, "lines": lines})
