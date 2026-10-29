"""Map Shopify-style webhook payloads to core-table upserts.

Pure functions: payload in, `Changes` out. The only I/O is through the `Lookups`
protocol (resolve an existing customer, check an order exists), which ingest.py backs
with Databricks and tests back with dicts.

Fields honored are listed per mapper; everything else in the payload is ignored (but
the full payload is always kept in raw.webhook_events).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Literal, Protocol

from app.tables import make_id, shopify_gid

log = logging.getLogger(__name__)

PROVIDER = "shopify"
CENT = Decimal("0.01")
Row = dict[str, Any]


class MappingError(ValueError):
    """Payload is valid JSON but cannot be applied (missing ids, unknown parent, ...)."""


class Lookups(Protocol):
    def customer_id_for(self, provider: str, provider_customer_id: str | None, email: str | None) -> str | None: ...

    def order_exists(self, order_id: str) -> bool: ...


@dataclass
class Upsert:
    table: str
    key: str
    rows: list[Row]
    mode: Literal["upsert", "insert_only"] = "upsert"
    # Skip the update when the incoming row is older than the stored one.
    guard: str | None = None
    # Columns written on insert but never overwritten on update.
    keep_on_update: tuple[str, ...] = ()
    # Values used on insert when the payload does not provide one.
    insert_defaults: dict[str, Any] = field(default_factory=dict)


@dataclass
class Changes:
    upserts: list[Upsert] = field(default_factory=list)
    # order_ids whose financial status must be recomputed from core.refunds afterwards.
    recompute_refund_status: list[str] = field(default_factory=list)

    def add(self, upsert: Upsert) -> None:
        if upsert.rows:
            self.upserts.append(upsert)


# --------------------------------------------------------------------------- helpers
def money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise MappingError(f"invalid money amount {value!r}") from exc


def ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MappingError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def gid(obj: dict[str, Any] | None, resource: str, id_key: str = "id") -> str | None:
    """Prefer admin_graphql_api_id; fall back to building one from the numeric id."""
    if not obj:
        return None
    if obj.get("admin_graphql_api_id"):
        return str(obj["admin_graphql_api_id"])
    if obj.get(id_key) is not None:
        return shopify_gid(resource, obj[id_key])
    return None


def ref_gid(value: Any, resource: str) -> str | None:
    if value is None or value == "":
        return None
    text = str(value)
    return text if text.startswith("gid://") else shopify_gid(resource, text)


def _strip_html(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip() or None


US_REGIONS = {
    "West": {"AK", "AZ", "CA", "CO", "HI", "ID", "MT", "NV", "NM", "OR", "UT", "WA", "WY"},
    "North": {"IL", "IN", "IA", "KS", "MI", "MN", "MO", "NE", "ND", "OH", "SD", "WI"},
    "South": {"AL", "AR", "FL", "GA", "KY", "LA", "MS", "NC", "OK", "SC", "TN", "TX", "VA", "WV"},
    "East": {"CT", "DE", "DC", "ME", "MD", "MA", "NH", "NJ", "NY", "PA", "RI", "VT"},
}
CA_REGIONS = {"West": {"BC", "AB", "SK", "MB"}, "East": {"ON", "QC", "NB", "NS", "PE", "NL"}, "North": {"YT", "NT", "NU"}}


def region_for(country: str | None, province: str | None) -> str | None:
    table = {"US": US_REGIONS, "CA": CA_REGIONS}.get((country or "").upper())
    if not table or not province:
        return None
    for region, codes in table.items():
        if province.upper() in codes:
            return region
    return None


def segment_from_tags(tags: Any) -> str | None:
    """Shopify has no segment field; honor a tag like `segment:SMB` or a bare `Enterprise`."""
    parts = tags if isinstance(tags, list) else str(tags or "").split(",")
    for raw in parts:
        tag = raw.strip().lower().removeprefix("segment:").strip()
        if tag in ("consumer", "smb", "enterprise"):
            return {"consumer": "Consumer", "smb": "SMB", "enterprise": "Enterprise"}[tag]
    return None


# --------------------------------------------------------------------------- customers
def _customer_row(c: dict[str, Any], lookups: Lookups, fallback_email: str | None = None) -> Row | None:
    provider_id = gid(c, "Customer")
    email = (c.get("email") or fallback_email or "").strip().lower() or None
    if not provider_id and not email:
        return None
    customer_id = lookups.customer_id_for(PROVIDER, provider_id, email) or make_id(
        "cust", PROVIDER, provider_id or f"email:{email}"
    )
    addr = c.get("default_address") or {}
    country = addr.get("country_code")
    consent = (c.get("email_marketing_consent") or {}).get("state")
    opt_in = c.get("accepts_marketing")
    if consent is not None:
        opt_in = consent == "subscribed"
    state = c.get("state")
    return {
        "customer_id": customer_id,
        "provider": PROVIDER,
        "provider_customer_id": provider_id,
        "email": email,
        "first_name": c.get("first_name"),
        "last_name": c.get("last_name"),
        "phone": c.get("phone") or addr.get("phone"),
        "region": region_for(country, addr.get("province_code")),
        "country": country,
        "segment": segment_from_tags(c.get("tags")),
        "marketing_opt_in": opt_in,
        "created_at": ts(c.get("created_at")),
        "updated_at": ts(c.get("updated_at")),
        "status": None if state is None else ("disabled" if state == "disabled" else "active"),
    }


def _customer_upsert(rows: list[Row]) -> Upsert:
    return Upsert(
        "customers", "customer_id", rows,
        guard="updated_at",
        keep_on_update=("created_at", "provider"),
        insert_defaults={"segment": "Consumer", "status": "active"},
    )


def _address_row(customer_id: str, addr: dict[str, Any] | None, addr_type: str) -> Row | None:
    if not addr or not (addr.get("address1") or addr.get("city")):
        return None
    return {
        "address_id": make_id("addr", customer_id, addr_type),
        "customer_id": customer_id,
        "type": addr_type,
        "line1": " ".join(p for p in (addr.get("address1"), addr.get("address2")) if p) or None,
        "city": addr.get("city"),
        "state": addr.get("province_code") or addr.get("province"),
        "postal_code": addr.get("zip"),
        "country": addr.get("country_code"),
        "is_default": True,
    }


def map_customer(payload: dict[str, Any], lookups: Lookups, default_currency: str = "USD") -> Changes:
    """customers/create, customers/update.

    Honors: id/admin_graphql_api_id, email, first_name, last_name, phone, state,
    accepts_marketing / email_marketing_consent.state, tags (segment), created_at,
    updated_at, default_address{address1,address2,city,province_code,zip,country_code}.
    """
    row = _customer_row(payload, lookups)
    if row is None:
        raise MappingError("customer payload has neither id nor email")
    changes = Changes()
    changes.add(_customer_upsert([row]))
    addr = _address_row(row["customer_id"], payload.get("default_address"), "shipping")
    if addr:
        changes.add(Upsert("addresses", "address_id", [addr]))
    return changes


# --------------------------------------------------------------------------- orders
def _financial(value: str | None) -> str:
    v = (value or "pending").lower()
    return {"partially_paid": "paid"}.get(v, v) if v in {
        "pending", "authorized", "partially_paid", "paid", "partially_refunded", "refunded", "voided"
    } else "pending"


def _fulfillment(value: str | None) -> str:
    v = (value or "").lower()
    return v if v in ("fulfilled", "partial") else "unfulfilled"


def order_status(financial: str, fulfillment: str, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if financial in ("refunded", "partially_refunded"):
        return financial
    if fulfillment == "fulfilled":
        return "fulfilled"
    if fulfillment == "partial":
        return "partially_fulfilled"
    if financial == "paid":
        return "paid"
    return "pending"


def _source(value: str | None) -> str:
    v = (value or "web").lower()
    if v == "pos":
        return "pos"
    if "draft" in v:
        return "draft"
    return "online"


def _payment_method(gateway: str | None) -> str:
    g = (gateway or "").lower()
    if "shopify_payments" in g:
        return "shopify_payments"
    if "paypal" in g:
        return "paypal"
    if "gift" in g:
        return "gift_card"
    if any(k in g for k in ("card", "stripe", "authorize", "braintree", "bogus")):
        return "card"
    return "other"


def _payment_status(kind: str | None, status: str | None) -> str | None:
    kind, status = (kind or "").lower(), (status or "").lower()
    if kind in ("refund", "change"):
        return None  # refunds are tracked in core.refunds
    if status in ("failure", "error"):
        return "failed"
    if kind == "void":
        return "voided"
    if kind == "authorization":
        return "authorized" if status == "success" else None
    if kind in ("sale", "capture") and status == "success":
        return "captured"
    return None


def map_order(payload: dict[str, Any], lookups: Lookups, default_currency: str = "USD") -> Changes:
    """orders/create, orders/updated.

    Honors: id/admin_graphql_api_id, name|order_number, email, currency, financial_status,
    fulfillment_status, source_name, created_at, updated_at, cancelled_at, customer{...},
    shipping_address, billing_address, shipping_lines[].price,
    line_items[]{id, product_id, variant_id, sku, title, variant_title, quantity, price,
    total_discount | discount_allocations[].amount, tax_lines[].price, fulfillment_status},
    transactions[]{id, gateway, kind, status, amount, processed_at} (optional; synthesized
    from financial_status when absent), fulfillments[]{id, status, tracking_company,
    tracking_number, created_at}.

    Monetary header fields are recomputed from line items so that
    total = subtotal - discount + tax + shipping always holds.
    """
    order_gid = gid(payload, "Order")
    if not order_gid:
        raise MappingError("order payload has no id")
    order_id = make_id("ord", PROVIDER, order_gid)
    currency = payload.get("currency") or default_currency
    changes = Changes()

    # ---- customer + addresses
    customer_id: str | None = None
    customer_payload = payload.get("customer") or {}
    if customer_payload or payload.get("email"):
        c_row = _customer_row(customer_payload, lookups, fallback_email=payload.get("email"))
        if c_row:
            if not c_row["country"]:
                ship = payload.get("shipping_address") or payload.get("billing_address") or {}
                c_row["country"] = ship.get("country_code")
                c_row["region"] = region_for(ship.get("country_code"), ship.get("province_code"))
            customer_id = c_row["customer_id"]
            changes.add(_customer_upsert([c_row]))

    address_ids: dict[str, str | None] = {"shipping": None, "billing": None}
    if customer_id:
        addr_rows = []
        for addr_type in ("shipping", "billing"):
            a = _address_row(customer_id, payload.get(f"{addr_type}_address"), addr_type)
            if a:
                addr_rows.append(a)
                address_ids[addr_type] = a["address_id"]
        changes.add(Upsert("addresses", "address_id", addr_rows))

    # ---- line items (+ stub items/variants so FKs hold for unseen products)
    lines: list[Row] = []
    stub_items: list[Row] = []
    stub_variants: list[Row] = []
    for li in payload.get("line_items") or []:
        li_gid = gid(li, "LineItem")
        if not li_gid:
            raise MappingError("line item without id")
        qty = int(li.get("quantity") or 0)
        price = money(li.get("price")) or Decimal("0.00")
        if li.get("discount_allocations"):
            disc = money(sum(Decimal(str(d.get("amount") or 0)) for d in li["discount_allocations"]))
        else:
            disc = money(li.get("total_discount")) or Decimal("0.00")
        tax = money(sum(Decimal(str(t.get("price") or 0)) for t in li.get("tax_lines") or [])) or Decimal("0.00")
        product_gid = ref_gid(li.get("product_id"), "Product")
        variant_gid = ref_gid(li.get("variant_id"), "ProductVariant")
        item_id = make_id("item", PROVIDER, product_gid) if product_gid else None
        variant_id = make_id("var", PROVIDER, variant_gid) if variant_gid else None
        title = li.get("name") or " - ".join(p for p in (li.get("title"), li.get("variant_title")) if p)
        if item_id:
            stub_items.append({
                "item_id": item_id, "provider": PROVIDER, "provider_product_id": product_gid,
                "provider_variant_id": variant_gid, "sku": li.get("sku"), "name": li.get("title"),
                "status": "active", "created_at": ts(payload.get("created_at")),
                "updated_at": ts(payload.get("created_at")),
            })
        if item_id and variant_id:
            stub_variants.append({
                "variant_id": variant_id, "item_id": item_id, "provider_variant_id": variant_gid,
                "sku": li.get("sku"), "title": li.get("variant_title"), "price": price, "currency": currency,
                "status": "active",
            })
        lines.append({
            "order_detail_id": make_id("od", PROVIDER, li_gid),
            "order_id": order_id,
            "item_id": item_id,
            "variant_id": variant_id,
            "sku": li.get("sku"),
            "title": title,
            "quantity": qty,
            "unit_price": price,
            "line_discount": disc,
            "line_tax": tax,
            "line_total": money(price * qty - disc + tax),
            "fulfillment_status": "fulfilled" if li.get("fulfillment_status") == "fulfilled" else "unfulfilled",
        })
    changes.add(Upsert("items", "item_id", stub_items, mode="insert_only"))
    changes.add(Upsert("item_variants", "variant_id", stub_variants, mode="insert_only"))

    # ---- header
    zero = Decimal("0.00")
    subtotal = money(sum((l["unit_price"] * l["quantity"] for l in lines), zero)) or zero
    discount = money(sum((l["line_discount"] for l in lines), zero)) or zero
    tax = money(sum((l["line_tax"] for l in lines), zero)) or zero
    shipping = money(sum(Decimal(str(s.get("price") or 0)) for s in payload.get("shipping_lines") or [])) or zero
    total = money(subtotal - discount + tax + shipping) or zero
    provider_total = money(payload.get("total_price"))
    if provider_total is not None and abs(provider_total - total) > CENT:
        log.warning("order total mismatch", extra={"order_id": order_id, "provider_total": str(provider_total), "computed_total": str(total)})

    financial = _financial(payload.get("financial_status"))
    fulfillment = _fulfillment(payload.get("fulfillment_status"))
    cancelled_at = ts(payload.get("cancelled_at"))
    ordered_at = ts(payload.get("processed_at") or payload.get("created_at"))
    updated_at = ts(payload.get("updated_at")) or ordered_at
    order_number = payload.get("name") or (f"#{payload['order_number']}" if payload.get("order_number") else None)
    changes.add(Upsert(
        "orders", "order_id",
        [{
            "order_id": order_id,
            "provider": PROVIDER,
            "provider_order_id": order_gid,
            "customer_id": customer_id,
            "order_number": order_number,
            "status": order_status(financial, fulfillment, cancelled_at is not None),
            "financial_status": financial,
            "fulfillment_status": fulfillment,
            "currency": currency,
            "subtotal": subtotal,
            "tax": tax,
            "shipping": shipping,
            "discount": discount,
            "total": total,
            "source": _source(payload.get("source_name")),
            "ordered_at": ordered_at,
            "cancelled_at": cancelled_at,
            "updated_at": updated_at,
            "shipping_address_id": address_ids["shipping"],
            "billing_address_id": address_ids["billing"],
        }],
        guard="updated_at",
        keep_on_update=("provider", "ordered_at"),
    ))
    changes.add(Upsert("order_details", "order_detail_id", lines))

    # ---- payments
    payments: list[Row] = []
    for t in payload.get("transactions") or []:
        status = _payment_status(t.get("kind"), t.get("status"))
        t_gid = gid(t, "OrderTransaction")
        if status is None or not t_gid:
            continue
        payments.append({
            "payment_id": make_id("pay", PROVIDER, t_gid), "order_id": order_id, "provider": PROVIDER,
            "provider_payment_id": t_gid, "method": _payment_method(t.get("gateway")),
            "amount": money(t.get("amount")), "currency": t.get("currency") or currency, "status": status,
            "processed_at": ts(t.get("processed_at") or t.get("created_at")),
        })
    changes.add(Upsert("payments", "payment_id", payments))
    if not payload.get("transactions"):
        # Order webhooks usually omit transactions. Synthesize one payment from financial_status,
        # keyed on order_id + insert-only: it is added only if the order has no payment yet.
        synthesized = {"paid": "captured", "partially_refunded": "captured", "refunded": "captured",
                       "authorized": "authorized", "voided": "voided"}.get(financial)
        if synthesized:
            gateways = payload.get("payment_gateway_names") or [payload.get("gateway")]
            changes.add(Upsert("payments", "order_id", [{
                "payment_id": make_id("pay", PROVIDER, order_gid, "1"), "order_id": order_id, "provider": PROVIDER,
                "provider_payment_id": None, "method": _payment_method(gateways[0] if gateways else None),
                "amount": total, "currency": currency, "status": synthesized, "processed_at": ordered_at,
            }], mode="insert_only"))

    # ---- fulfillments
    fulfillments: list[Row] = []
    for f in payload.get("fulfillments") or []:
        f_gid = gid(f, "Fulfillment")
        if not f_gid:
            continue
        status = (f.get("status") or "success").lower()
        fulfillments.append({
            "fulfillment_id": make_id("ful", PROVIDER, f_gid), "order_id": order_id,
            "status": status if status in ("pending", "success", "cancelled", "error") else "pending",
            "tracking_company": f.get("tracking_company"),
            "tracking_number": f.get("tracking_number") or next(iter(f.get("tracking_numbers") or []), None),
            "shipped_at": ts(f.get("created_at")), "delivered_at": ts(f.get("delivered_at")),
        })
    changes.add(Upsert("fulfillments", "fulfillment_id", fulfillments))
    return changes


# --------------------------------------------------------------------------- refunds
def map_refund(payload: dict[str, Any], lookups: Lookups, default_currency: str = "USD") -> Changes:
    """refunds/create.

    Honors: id/admin_graphql_api_id, order_id, created_at|processed_at, note, reason
    (non-standard, optional), refund_line_items[]{line_item_id, quantity, subtotal,
    total_tax, restock_type}, transactions[]{amount, currency, kind, status}.
    Afterwards the order's financial_status/status are recomputed from core.refunds.
    """
    refund_gid = gid(payload, "Refund")
    order_gid = ref_gid(payload.get("order_id"), "Order")
    if not refund_gid or not order_gid:
        raise MappingError("refund payload needs id and order_id")
    order_id = make_id("ord", PROVIDER, order_gid)
    if not lookups.order_exists(order_id):
        raise MappingError(f"refund for unknown order {order_gid}; will succeed on retry once the order exists")
    refund_id = make_id("ref", PROVIDER, refund_gid)

    details: list[Row] = []
    restock_types: set[str] = set()
    for rli in payload.get("refund_line_items") or []:
        li_gid = ref_gid(rli.get("line_item_id"), "LineItem")
        if not li_gid:
            continue
        restock_type = (rli.get("restock_type") or "no_restock").lower()
        restock_types.add(restock_type)
        od_id = make_id("od", PROVIDER, li_gid)
        details.append({
            "refund_detail_id": make_id("rd", refund_id, od_id),
            "refund_id": refund_id,
            "order_detail_id": od_id,
            "quantity": int(rli.get("quantity") or 0),
            "amount": money(Decimal(str(rli.get("subtotal") or 0)) + Decimal(str(rli.get("total_tax") or 0))),
            "restock": restock_type in ("return", "cancel", "legacy_restock"),
        })

    txns = [t for t in payload.get("transactions") or [] if (t.get("kind") or "refund").lower() == "refund"]
    statuses = {(t.get("status") or "").lower() for t in txns}
    if txns:
        amount = money(sum(Decimal(str(t.get("amount") or 0)) for t in txns if (t.get("status") or "").lower() != "failure"))
        status = "succeeded" if "success" in statuses else ("pending" if "pending" in statuses else "failed")
        currency = txns[0].get("currency") or default_currency
    else:
        amount = money(sum((d["amount"] for d in details), Decimal(0)))
        status, currency = "succeeded", default_currency

    reason = (payload.get("reason") or "").lower()
    if reason not in ("customer", "inventory", "other"):
        reason = "customer" if "return" in restock_types else ("inventory" if "cancel" in restock_types else "other")

    changes = Changes()
    changes.add(Upsert("refunds", "refund_id", [{
        "refund_id": refund_id, "order_id": order_id, "provider": PROVIDER, "provider_refund_id": refund_gid,
        "amount": amount, "currency": currency, "reason": reason, "status": status,
        "refunded_at": ts(payload.get("processed_at") or payload.get("created_at")), "note": payload.get("note"),
    }]))
    changes.add(Upsert("refund_details", "refund_detail_id", details))
    changes.recompute_refund_status.append(order_id)
    return changes


# --------------------------------------------------------------------------- products
def map_product(payload: dict[str, Any], lookups: Lookups, default_currency: str = "USD") -> Changes:
    """products/create, products/update.

    Honors: id/admin_graphql_api_id, title, body_html, vendor, product_type (-> category),
    status, created_at, updated_at, variants[]{id, sku, title, price, inventory_quantity,
    grams, barcode}. `cost` is not in Shopify product webhooks, so existing cost is kept.
    """
    product_gid = gid(payload, "Product")
    if not product_gid:
        raise MappingError("product payload has no id")
    item_id = make_id("item", PROVIDER, product_gid)
    status = (payload.get("status") or "active").lower()
    status = status if status in ("active", "archived", "draft") else "active"
    variants: list[Row] = []
    for v in payload.get("variants") or []:
        v_gid = gid(v, "ProductVariant")
        if not v_gid:
            continue
        variants.append({
            "variant_id": make_id("var", PROVIDER, v_gid), "item_id": item_id, "provider_variant_id": v_gid,
            "sku": v.get("sku"), "title": v.get("title"), "price": money(v.get("price")),
            "currency": default_currency, "cost": None,
            "inventory_quantity": None if v.get("inventory_quantity") is None else int(v["inventory_quantity"]),
            "weight_grams": None if v.get("grams") is None else int(v["grams"]),
            "barcode": v.get("barcode"), "status": "active" if status == "active" else "archived",
        })
    changes = Changes()
    changes.add(Upsert("items", "item_id", [{
        "item_id": item_id, "provider": PROVIDER, "provider_product_id": product_gid,
        "provider_variant_id": variants[0]["provider_variant_id"] if variants else None,
        "sku": variants[0]["sku"] if variants else None, "name": payload.get("title"),
        "description": _strip_html(payload.get("body_html")), "category": payload.get("product_type") or None,
        "brand": payload.get("vendor"), "status": status,
        "created_at": ts(payload.get("created_at")), "updated_at": ts(payload.get("updated_at")),
    }], guard="updated_at", keep_on_update=("created_at", "provider")))
    changes.add(Upsert("item_variants", "variant_id", variants))
    return changes


Mapper = Callable[[dict[str, Any], Lookups, str], Changes]

MAPPERS: dict[str, Mapper] = {
    "orders/create": map_order,
    "orders/updated": map_order,
    "orders/paid": map_order,
    "orders/fulfilled": map_order,
    "orders/cancelled": map_order,
    "refunds/create": map_refund,
    "products/create": map_product,
    "products/update": map_product,
    "customers/create": map_customer,
    "customers/update": map_customer,
}
