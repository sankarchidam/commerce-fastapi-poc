from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import DbDep, PageDep, SessionDep, paginate, require_api_key
from app.models import Fulfillment, OrderDetail, OrderLine, OrderSummary, Page, Payment, RefundDetail, RefundLine

router = APIRouter(prefix="/orders", tags=["orders"], dependencies=[Depends(require_api_key)])

OrderStatus = Literal[
    "pending", "paid", "fulfilled", "partially_fulfilled", "cancelled", "refunded", "partially_refunded"
]
FinancialStatus = Literal["pending", "authorized", "paid", "partially_refunded", "refunded", "voided"]


@router.get("", response_model=Page[OrderSummary], summary="List orders, newest first")
def list_orders(
    s: SessionDep,
    db: DbDep,
    page: PageDep,
    customer_id: str | None = None,
    status: OrderStatus | None = None,
    financial_status: FinancialStatus | None = None,
    provider_order_id: Annotated[str | None, Query(examples=["gid://shopify/Order/5600000042"])] = None,
    date_from: Annotated[date | None, Query(description="Inclusive, UTC (YYYY-MM-DD)")] = None,
    date_to: Annotated[date | None, Query(description="Inclusive, UTC (YYYY-MM-DD)")] = None,
) -> Page[OrderSummary]:
    params = {
        "customer_id": customer_id,
        "status": status,
        "financial_status": financial_status,
        "provider_order_id": provider_order_id,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    rows, total = paginate(s, db, "orders_list", params, "ordered_at DESC, order_id", page)
    return Page[OrderSummary](
        items=[OrderSummary.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.get("/{order_id}", response_model=OrderDetail, summary="Order header, lines, payments, fulfillments, refunds")
def get_order(order_id: str, s: SessionDep, db: DbDep) -> OrderDetail:
    p = {"order_id": order_id}
    header = s.fetch_one(db.query("orders_get"), p)
    if header is None:
        raise HTTPException(404, f"order {order_id} not found")
    detail = OrderDetail.model_validate(header)
    detail.lines = [OrderLine.model_validate(r) for r in s.fetch_all(db.query("order_lines"), p)]
    detail.payments = [Payment.model_validate(r) for r in s.fetch_all(db.query("order_payments"), p)]
    detail.fulfillments = [Fulfillment.model_validate(r) for r in s.fetch_all(db.query("order_fulfillments"), p)]
    refunds = s.fetch_all(db.query("order_refunds"), p)
    if refunds:
        lines_by_refund: dict[str, list[RefundLine]] = defaultdict(list)
        for r in s.fetch_all(db.query("order_refund_lines"), p):
            lines_by_refund[r["refund_id"]].append(RefundLine.model_validate(r))
        detail.refunds = [
            RefundDetail.model_validate({**r, "lines": lines_by_refund.get(r["refund_id"], [])}) for r in refunds
        ]
    return detail
