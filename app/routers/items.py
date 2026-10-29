from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import DbDep, PageDep, SessionDep, like_pattern, paginate, require_api_key
from app.models import Item, ItemDetail, ItemVariant, Page

router = APIRouter(prefix="/items", tags=["items"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=Page[Item], summary="List items (products)")
def list_items(
    s: SessionDep,
    db: DbDep,
    page: PageDep,
    category: Annotated[str | None, Query(examples=["Hair Care"])] = None,
    status: Literal["active", "archived", "draft"] | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Search name or SKU")] = None,
) -> Page[Item]:
    params = {"category": category, "status": status, "q": like_pattern(q)}
    rows, total = paginate(s, db, "items_list", params, "name, item_id", page)
    return Page[Item](items=[Item.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{item_id}", response_model=ItemDetail, summary="Item with variants and inventory")
def get_item(item_id: str, s: SessionDep, db: DbDep) -> ItemDetail:
    row = s.fetch_one(db.query("items_get"), {"item_id": item_id})
    if row is None:
        raise HTTPException(404, f"item {item_id} not found")
    variants = [ItemVariant.model_validate(v) for v in s.fetch_all(db.query("item_variants"), {"item_id": item_id})]
    detail = ItemDetail.model_validate(row)
    detail.variants = variants
    detail.total_inventory = sum(v.inventory_quantity or 0 for v in variants)
    return detail
