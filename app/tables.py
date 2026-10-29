"""Column specs for the core/raw tables, used by the loader and the MERGE builder.

Must match sql/ddl.sql (tests/test_ddl_consistency.py checks this).
"""

from __future__ import annotations

import hashlib

Columns = list[tuple[str, str]]

CORE_TABLES: dict[str, Columns] = {
    "customers": [
        ("customer_id", "STRING"),
        ("provider", "STRING"),
        ("provider_customer_id", "STRING"),
        ("email", "STRING"),
        ("first_name", "STRING"),
        ("last_name", "STRING"),
        ("phone", "STRING"),
        ("region", "STRING"),
        ("country", "STRING"),
        ("segment", "STRING"),
        ("marketing_opt_in", "BOOLEAN"),
        ("created_at", "TIMESTAMP"),
        ("updated_at", "TIMESTAMP"),
        ("status", "STRING"),
    ],
    "addresses": [
        ("address_id", "STRING"),
        ("customer_id", "STRING"),
        ("type", "STRING"),
        ("line1", "STRING"),
        ("city", "STRING"),
        ("state", "STRING"),
        ("postal_code", "STRING"),
        ("country", "STRING"),
        ("is_default", "BOOLEAN"),
    ],
    "items": [
        ("item_id", "STRING"),
        ("provider", "STRING"),
        ("provider_product_id", "STRING"),
        ("provider_variant_id", "STRING"),
        ("sku", "STRING"),
        ("name", "STRING"),
        ("description", "STRING"),
        ("category", "STRING"),
        ("brand", "STRING"),
        ("status", "STRING"),
        ("created_at", "TIMESTAMP"),
        ("updated_at", "TIMESTAMP"),
    ],
    "item_variants": [
        ("variant_id", "STRING"),
        ("item_id", "STRING"),
        ("provider_variant_id", "STRING"),
        ("sku", "STRING"),
        ("title", "STRING"),
        ("price", "DECIMAL(18,2)"),
        ("currency", "STRING"),
        ("cost", "DECIMAL(18,2)"),
        ("inventory_quantity", "INT"),
        ("weight_grams", "INT"),
        ("barcode", "STRING"),
        ("status", "STRING"),
    ],
    "orders": [
        ("order_id", "STRING"),
        ("provider", "STRING"),
        ("provider_order_id", "STRING"),
        ("customer_id", "STRING"),
        ("order_number", "STRING"),
        ("status", "STRING"),
        ("financial_status", "STRING"),
        ("fulfillment_status", "STRING"),
        ("currency", "STRING"),
        ("subtotal", "DECIMAL(18,2)"),
        ("tax", "DECIMAL(18,2)"),
        ("shipping", "DECIMAL(18,2)"),
        ("discount", "DECIMAL(18,2)"),
        ("total", "DECIMAL(18,2)"),
        ("source", "STRING"),
        ("ordered_at", "TIMESTAMP"),
        ("cancelled_at", "TIMESTAMP"),
        ("updated_at", "TIMESTAMP"),
        ("shipping_address_id", "STRING"),
        ("billing_address_id", "STRING"),
    ],
    "order_details": [
        ("order_detail_id", "STRING"),
        ("order_id", "STRING"),
        ("item_id", "STRING"),
        ("variant_id", "STRING"),
        ("sku", "STRING"),
        ("title", "STRING"),
        ("quantity", "INT"),
        ("unit_price", "DECIMAL(18,2)"),
        ("line_discount", "DECIMAL(18,2)"),
        ("line_tax", "DECIMAL(18,2)"),
        ("line_total", "DECIMAL(18,2)"),
        ("fulfillment_status", "STRING"),
    ],
    "payments": [
        ("payment_id", "STRING"),
        ("order_id", "STRING"),
        ("provider", "STRING"),
        ("provider_payment_id", "STRING"),
        ("method", "STRING"),
        ("amount", "DECIMAL(18,2)"),
        ("currency", "STRING"),
        ("status", "STRING"),
        ("processed_at", "TIMESTAMP"),
    ],
    "fulfillments": [
        ("fulfillment_id", "STRING"),
        ("order_id", "STRING"),
        ("status", "STRING"),
        ("tracking_company", "STRING"),
        ("tracking_number", "STRING"),
        ("shipped_at", "TIMESTAMP"),
        ("delivered_at", "TIMESTAMP"),
    ],
    "refunds": [
        ("refund_id", "STRING"),
        ("order_id", "STRING"),
        ("provider", "STRING"),
        ("provider_refund_id", "STRING"),
        ("amount", "DECIMAL(18,2)"),
        ("currency", "STRING"),
        ("reason", "STRING"),
        ("status", "STRING"),
        ("refunded_at", "TIMESTAMP"),
        ("note", "STRING"),
    ],
    "refund_details": [
        ("refund_detail_id", "STRING"),
        ("refund_id", "STRING"),
        ("order_detail_id", "STRING"),
        ("quantity", "INT"),
        ("amount", "DECIMAL(18,2)"),
        ("restock", "BOOLEAN"),
    ],
    "inventory_movements": [
        ("movement_id", "STRING"),
        ("variant_id", "STRING"),
        ("order_id", "STRING"),
        ("quantity_delta", "INT"),
        ("reason", "STRING"),
        ("occurred_at", "TIMESTAMP"),
    ],
}

RAW_TABLES: dict[str, Columns] = {
    "webhook_events": [
        ("event_id", "STRING"),
        ("provider", "STRING"),
        ("topic", "STRING"),
        ("shop_domain", "STRING"),
        ("hmac_valid", "BOOLEAN"),
        ("payload_json", "STRING"),
        ("processing_status", "STRING"),
        ("error_message", "STRING"),
        ("received_at", "TIMESTAMP"),
        ("processed_at", "TIMESTAMP"),
    ],
}

# Reference data seeded by sql/ddl.sql itself (MERGE), so the loader never truncates it.
REFERENCE_TABLES: dict[str, Columns] = {
    "fx_rates": [
        ("currency", "STRING"),
        ("rate_to_usd", "DECIMAL(18,6)"),
        ("updated_at", "TIMESTAMP"),
    ],
}

# Load order respects FK direction (parents first).
CORE_LOAD_ORDER = [
    "customers",
    "addresses",
    "items",
    "item_variants",
    "orders",
    "order_details",
    "payments",
    "fulfillments",
    "refunds",
    "refund_details",
    "inventory_movements",
]


def column_names(table: str) -> list[str]:
    spec = CORE_TABLES.get(table) or RAW_TABLES[table]
    return [name for name, _ in spec]


def make_id(prefix: str, *parts: object) -> str:
    """Deterministic surrogate id, e.g. make_id("ord", "shopify", gid) -> "ord_3f2a...".

    Both the seed generator and the webhook mapper derive internal ids this way, so a
    webhook about a seeded order lands on the same row.
    """
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def shopify_gid(resource: str, numeric_id: object) -> str:
    return f"gid://shopify/{resource}/{numeric_id}"
