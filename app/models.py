"""Request/response schemas. Money is Decimal serialized as a two-decimal string."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer


def _to_money(value: Any) -> Any:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _to_utc(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(_to_money),
    PlainSerializer(lambda d: f"{d:.2f}", return_type=str, when_used="json"),
]
UtcDatetime = Annotated[
    datetime,
    BeforeValidator(_to_utc),
    PlainSerializer(lambda d: d.astimezone(UTC).isoformat().replace("+00:00", "Z"), return_type=str, when_used="json"),
]

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int = Field(description="Total rows matching the filters, ignoring limit/offset")
    limit: int
    offset: int


# ---------------------------------------------------------------- health
class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    service: str = "commerce-fastapi-poc"


class DatabricksHealth(ApiModel):
    status: Literal["ok", "error"]
    host: str
    catalog: str
    core_schema_visible: bool = False
    raw_schema_visible: bool = False
    core_tables: list[str] = []
    latency_ms: float | None = None
    error: str | None = None


# ---------------------------------------------------------------- customers
class Address(ApiModel):
    address_id: str
    type: str | None = None
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    is_default: bool | None = None


class Customer(ApiModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "customer_id": "cust_3f9a1c2b7d4e5f60", "provider": "shopify",
        "provider_customer_id": "gid://shopify/Customer/7100000000042", "email": "maya.patel@gmail.com",
        "first_name": "Maya", "last_name": "Patel", "phone": "+1-415-555-0142", "region": "West",
        "country": "US", "segment": "Consumer", "marketing_opt_in": True, "status": "active",
        "created_at": "2025-03-14T09:12:44Z", "updated_at": "2026-01-02T18:40:01Z",
    }]})

    customer_id: str
    provider: str | None = None
    provider_customer_id: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    region: str | None = None
    country: str | None = None
    segment: str | None = None
    marketing_opt_in: bool | None = None
    status: str | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class CustomerDetail(Customer):
    addresses: list[Address] = []
    order_count: int = 0
    gross: Money = Decimal("0")
    refunds: Money = Decimal("0")
    net: Money = Decimal("0")
    first_order_at: UtcDatetime | None = None
    last_order_at: UtcDatetime | None = None


# ---------------------------------------------------------------- items
class Item(ApiModel):
    item_id: str
    provider: str | None = None
    provider_product_id: str | None = None
    provider_variant_id: str | None = None
    sku: str | None = None
    name: str | None = None
    description: str | None = None
    category: str | None = None
    brand: str | None = None
    status: str | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class ItemVariant(ApiModel):
    variant_id: str
    item_id: str | None = None
    provider_variant_id: str | None = None
    sku: str | None = None
    title: str | None = None
    price: Money | None = None
    currency: str | None = None
    cost: Money | None = None
    inventory_quantity: int | None = None
    weight_grams: int | None = None
    barcode: str | None = None
    status: str | None = None


class ItemDetail(Item):
    variants: list[ItemVariant] = []
    total_inventory: int = 0
    units_sold_30d: int = 0


# ---------------------------------------------------------------- orders
class OrderSummary(ApiModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "order_id": "ord_1b2c3d4e5f607182", "provider": "shopify",
        "provider_order_id": "gid://shopify/Order/5600001234", "customer_id": "cust_3f9a1c2b7d4e5f60",
        "customer_email": "maya.patel@gmail.com", "order_number": "#2235", "status": "fulfilled",
        "financial_status": "paid", "fulfillment_status": "fulfilled", "currency": "USD",
        "subtotal": "42.97", "tax": "3.01", "shipping": "5.99", "discount": "0.00", "total": "51.97",
        "source": "online", "item_count": 3, "refunded_amount": "0.00",
        "ordered_at": "2026-05-02T14:03:11Z", "cancelled_at": None, "updated_at": "2026-05-03T10:00:00Z",
    }]})

    order_id: str
    provider: str | None = None
    provider_order_id: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    order_number: str | None = None
    status: str | None = None
    financial_status: str | None = None
    fulfillment_status: str | None = None
    currency: str | None = None
    subtotal: Money | None = None
    tax: Money | None = None
    shipping: Money | None = None
    discount: Money | None = None
    total: Money | None = None
    source: str | None = None
    item_count: int = 0
    refunded_amount: Money = Decimal("0")
    ordered_at: UtcDatetime | None = None
    cancelled_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class OrderLine(ApiModel):
    order_detail_id: str
    item_id: str | None = None
    variant_id: str | None = None
    sku: str | None = None
    title: str | None = None
    quantity: int
    unit_price: Money
    line_discount: Money
    line_tax: Money
    line_total: Money
    fulfillment_status: str | None = None


class Payment(ApiModel):
    payment_id: str
    provider: str | None = None
    provider_payment_id: str | None = None
    method: str | None = None
    amount: Money
    currency: str | None = None
    status: str | None = None
    processed_at: UtcDatetime | None = None


class Fulfillment(ApiModel):
    fulfillment_id: str
    status: str | None = None
    tracking_company: str | None = None
    tracking_number: str | None = None
    shipped_at: UtcDatetime | None = None
    delivered_at: UtcDatetime | None = None


class RefundLine(ApiModel):
    refund_detail_id: str
    order_detail_id: str | None = None
    quantity: int | None = None
    amount: Money
    restock: bool | None = None


class Refund(ApiModel):
    refund_id: str
    order_id: str | None = None
    order_number: str | None = None
    provider: str | None = None
    provider_refund_id: str | None = None
    amount: Money
    currency: str | None = None
    reason: str | None = None
    status: str | None = None
    refunded_at: UtcDatetime | None = None
    note: str | None = None


class RefundDetail(Refund):
    lines: list[RefundLine] = []


class OrderDetail(OrderSummary):
    shipping_address_id: str | None = None
    billing_address_id: str | None = None
    lines: list[OrderLine] = []
    payments: list[Payment] = []
    fulfillments: list[Fulfillment] = []
    refunds: list[RefundDetail] = []


# ---------------------------------------------------------------- webhooks / stats
class WebhookEvent(ApiModel):
    event_id: str
    provider: str | None = None
    topic: str | None = None
    shop_domain: str | None = None
    hmac_valid: bool | None = None
    processing_status: str | None = None
    error_message: str | None = None
    payload_bytes: int | None = None
    received_at: UtcDatetime | None = None
    processed_at: UtcDatetime | None = None


class WebhookEventDetail(WebhookEvent):
    payload: Any = None


class WebhookAck(ApiModel):
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"status": "applied", "event_id": "b3f1c7e0-0001", "topic": "orders/create", "applied": {"orders": 1, "order_details": 2}},
        {"status": "duplicate", "event_id": "b3f1c7e0-0001", "topic": "orders/create", "applied": {}},
    ]})

    status: Literal["applied", "accepted", "duplicate", "skipped", "failed"]
    event_id: str
    topic: str
    applied: dict[str, int] = {}
    error: str | None = None


class StatsOverview(ApiModel):
    customers: int
    items: int
    orders: int
    refunds: int
    gmv: Money = Field(description="Sum of order totals, excluding cancelled orders (mixed currencies, POC)")
    refunded_amount: Money
    last_webhook_at: UtcDatetime | None = None
