from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import DbDep, PageDep, SessionDep, like_pattern, paginate, require_api_key
from app.models import Address, Customer, CustomerDetail, Page

router = APIRouter(prefix="/customers", tags=["customers"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=Page[Customer], summary="List customers")
def list_customers(
    s: SessionDep,
    db: DbDep,
    page: PageDep,
    email: Annotated[str | None, Query(description="Exact email (case-insensitive)")] = None,
    region: Literal["West", "East", "North", "South"] | None = None,
    segment: Literal["Consumer", "SMB", "Enterprise"] | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Search name or email")] = None,
) -> Page[Customer]:
    params = {"email": email, "region": region, "segment": segment, "q": like_pattern(q)}
    rows, total = paginate(s, db, "customers_list", params, "created_at DESC, customer_id", page)
    return Page[Customer](items=[Customer.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{customer_id}", response_model=CustomerDetail, summary="Customer profile, addresses and LTV")
def get_customer(customer_id: str, s: SessionDep, db: DbDep) -> CustomerDetail:
    row = s.fetch_one(db.query("customers_get"), {"customer_id": customer_id})
    if row is None:
        raise HTTPException(404, f"customer {customer_id} not found")
    addresses = s.fetch_all(db.query("customer_addresses"), {"customer_id": customer_id})
    detail = CustomerDetail.model_validate({k: v for k, v in row.items() if v is not None})
    detail.addresses = [Address.model_validate(a) for a in addresses]
    return detail
