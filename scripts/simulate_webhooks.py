"""Prove the webhook path end-to-end without a Shopify store.

Builds a Shopify-like order for a real seeded customer and real variants, HMAC-signs it,
POSTs it to the running API, then sends orders/updated (fulfilled), refunds/create and a
replay of the first delivery. Prints Databricks row counts before/after.

    uvicorn app.main:app &                 # in another terminal
    python -m scripts.simulate_webhooks
    python -m scripts.simulate_webhooks --base-url http://localhost:8000 --no-db   # skip Databricks reads
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import random
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.config import get_settings
from app.db import Database
from app.tables import make_id

TRACKED = ["customers", "orders", "order_details", "payments", "fulfillments", "refunds", "refund_details"]
TAX = {"US": Decimal("0.07"), "CA": Decimal("0.13"), "GB": Decimal("0.20")}
CURRENCY = {"US": "USD", "CA": "CAD", "GB": "GBP"}


def sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def numeric(gid: str) -> int:
    return int(gid.rsplit("/", 1)[-1])


def m(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def counts(db: Database) -> dict[str, int]:
    core = db.settings.core
    union = " UNION ALL ".join(f"SELECT '{t}' AS t, COUNT(*) AS n FROM {core}.`{t}`" for t in TRACKED)
    union += f" UNION ALL SELECT 'raw.webhook_events' AS t, COUNT(*) AS n FROM {db.settings.raw}.`webhook_events`"
    with db.session() as s:
        return {r["t"]: int(r["n"]) for r in s.fetch_all(union)}


def pick_seed_entities(db: Database, rng: random.Random) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    core = db.settings.core
    with db.session() as s:
        customer = s.fetch_one(
            f"""SELECT c.provider_customer_id, c.email, c.first_name, c.last_name, c.country,
                       a.line1, a.city, a.state, a.postal_code
                FROM {core}.customers c
                LEFT JOIN {core}.addresses a ON a.customer_id = c.customer_id AND a.type = 'shipping'
                WHERE c.provider = 'shopify' AND c.status = 'active' AND c.provider_customer_id IS NOT NULL
                ORDER BY rand({rng.randint(0, 10**6)}) LIMIT 1"""
        )
        variants = s.fetch_all(
            f"""SELECT i.provider_product_id, i.name, v.provider_variant_id, v.sku, v.title, v.price
                FROM {core}.item_variants v JOIN {core}.items i ON i.item_id = v.item_id
                WHERE i.status = 'active' AND i.provider = 'shopify'
                ORDER BY rand({rng.randint(0, 10**6)}) LIMIT {rng.randint(1, 3)}"""
        )
    if not customer or not variants:
        sys.exit("no seeded customers/variants found; run the bootstrap first (or use --no-db)")
    return customer, variants


def fallback_entities() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    customer = {"provider_customer_id": "gid://shopify/Customer/7100000000001", "email": "sim.shopper@example.com",
                "first_name": "Sim", "last_name": "Shopper", "country": "US", "line1": "500 Market St",
                "city": "San Francisco", "state": "CA", "postal_code": "94105"}
    variants = [{"provider_product_id": "gid://shopify/Product/8200000001", "name": "Lavender Shampoo",
                 "provider_variant_id": "gid://shopify/ProductVariant/4400000010", "sku": "HC-0001-1",
                 "title": "250 ml", "price": Decimal("9.99")}]
    return customer, variants


def build_order(customer: dict[str, Any], variants: list[dict[str, Any]], rng: random.Random) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    order_num = 5_690_000_000_000 + rng.randrange(10**9)
    country = customer["country"] or "US"
    rate = TAX.get(country, Decimal("0"))
    address = {"address1": customer["line1"], "city": customer["city"], "province_code": customer["state"],
               "zip": customer["postal_code"], "country_code": country}
    lines, subtotal, tax_total = [], Decimal(0), Decimal(0)
    for i, v in enumerate(variants):
        qty = rng.choice([1, 1, 2])
        price = Decimal(str(v["price"]))
        tax = (price * qty * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        subtotal += price * qty
        tax_total += tax
        lines.append({
            "id": order_num * 10 + i, "product_id": numeric(v["provider_product_id"]),
            "variant_id": numeric(v["provider_variant_id"]), "sku": v["sku"], "title": v["name"],
            "variant_title": v["title"], "name": f"{v['name']} - {v['title']}", "quantity": qty,
            "price": m(price), "total_discount": "0.00", "tax_lines": [{"price": m(tax), "rate": float(rate)}],
            "fulfillment_status": None,
        })
    shipping = Decimal("0") if subtotal >= 50 else Decimal("5.99")
    return {
        "id": order_num, "admin_graphql_api_id": f"gid://shopify/Order/{order_num}",
        "name": f"#SIM{order_num % 100000}", "email": customer["email"], "currency": CURRENCY.get(country, "USD"),
        "financial_status": "paid", "fulfillment_status": None, "source_name": "web",
        "created_at": now.isoformat(), "processed_at": now.isoformat(), "updated_at": now.isoformat(),
        "total_tax": m(tax_total), "total_price": m(subtotal + tax_total + shipping),
        "customer": {"id": numeric(customer["provider_customer_id"]), "email": customer["email"],
                     "first_name": customer["first_name"], "last_name": customer["last_name"],
                     "updated_at": now.isoformat()},
        "shipping_address": address, "billing_address": address,
        "shipping_lines": [{"title": "Standard", "price": m(shipping)}],
        "line_items": lines,
        "transactions": [{"id": order_num + 1, "kind": "sale", "status": "success", "gateway": "shopify_payments",
                          "amount": m(subtotal + tax_total + shipping), "currency": CURRENCY.get(country, "USD"),
                          "processed_at": now.isoformat()}],
    }


def fulfilled(order: dict[str, Any]) -> dict[str, Any]:
    updated = json.loads(json.dumps(order))
    ts = (datetime.fromisoformat(order["updated_at"]) + timedelta(hours=20)).isoformat()
    updated.update(fulfillment_status="fulfilled", updated_at=ts, fulfillments=[
        {"id": order["id"] + 2, "status": "success", "tracking_company": "UPS",
         "tracking_number": "1Z" + uuid.uuid4().hex[:16].upper(), "created_at": ts}])
    for li in updated["line_items"]:
        li["fulfillment_status"] = "fulfilled"
    updated.pop("transactions")
    return updated


def refund_for(order: dict[str, Any]) -> dict[str, Any]:
    li = order["line_items"][0]
    sub = Decimal(li["price"])
    tax = (Decimal(li["tax_lines"][0]["price"]) / li["quantity"]).quantize(Decimal("0.01"))
    return {
        "id": order["id"] + 3, "order_id": order["id"], "created_at": datetime.now(UTC).isoformat(),
        "note": "Simulated return: scent not as expected",
        "refund_line_items": [{"id": order["id"] + 4, "line_item_id": li["id"], "quantity": 1,
                               "restock_type": "return", "subtotal": m(sub), "total_tax": m(tax)}],
        "transactions": [{"id": order["id"] + 5, "kind": "refund", "status": "success",
                          "amount": m(sub + tax), "currency": order["currency"]}],
    }


def post(client: httpx.Client, secret: str, topic: str, payload: dict[str, Any], webhook_id: str) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    t0 = time.perf_counter()
    resp = client.post(
        f"/webhooks/shopify/{topic.replace('/', '_')}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Topic": topic,
            "X-Shopify-Hmac-Sha256": sign(body, secret),
            "X-Shopify-Webhook-Id": webhook_id,
            "X-Shopify-Shop-Domain": "commerce-poc-demo.myshopify.com",
        },
    )
    data = resp.json()
    ack_ms = (time.perf_counter() - t0) * 1000
    if data.get("status") == "accepted":  # WEBHOOK_APPLY_MODE=background: wait for the apply
        for _ in range(120):
            ev = client.get(f"/webhooks/events/{webhook_id}", headers={"X-API-Key": get_settings().api_key})
            if ev.status_code == 200 and ev.json()["processing_status"] not in ("received",):
                data = {**data, "status": f"accepted->{ev.json()['processing_status']}", "error": ev.json()["error_message"]}
                break
            time.sleep(0.5)
        print(f"  (acked in {ack_ms:.0f} ms)")
    print(f"  POST {topic:<16} -> {resp.status_code} {data.get('status'):<9} "
          f"{(time.perf_counter() - t0) * 1000:6.0f} ms  applied={data.get('applied')} {data.get('error') or ''}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--no-db", action="store_true", help="don't read Databricks; use built-in fake entities")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--bad-hmac", action="store_true", help="also send one forged request")
    args = parser.parse_args()

    settings = get_settings()
    rng = random.Random(args.seed)
    db = None if args.no_db else Database(settings)
    before = counts(db) if db else {}
    customer, variants = pick_seed_entities(db, rng) if db else fallback_entities()
    order = build_order(customer, variants, rng)
    first_id = f"sim-{uuid.uuid4()}"
    print(f"order {order['name']} for {customer['email']} with {len(variants)} line(s), total {order['total_price']}")

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        post(client, settings.shopify_webhook_secret, "orders/create", order, first_id)
        post(client, settings.shopify_webhook_secret, "orders/updated", fulfilled(order), f"sim-{uuid.uuid4()}")
        post(client, settings.shopify_webhook_secret, "refunds/create", refund_for(order), f"sim-{uuid.uuid4()}")
        print("  replaying the first delivery (same X-Shopify-Webhook-Id):")
        post(client, settings.shopify_webhook_secret, "orders/create", order, first_id)
        if args.bad_hmac:
            body = json.dumps(order).encode()
            r = client.post("/webhooks/shopify/orders_create", content=body,
                            headers={"X-Shopify-Hmac-Sha256": "forged", "X-Shopify-Webhook-Id": f"sim-{uuid.uuid4()}"})
            print(f"  POST forged HMAC     -> {r.status_code} {r.json()}")

        order_id = make_id("ord", "shopify", order["admin_graphql_api_id"])
        detail = client.get(f"/orders/{order_id}", headers={"X-API-Key": settings.api_key})
        if detail.status_code == 200:
            d = detail.json()
            print(f"\nGET /orders/{order_id} -> status={d['status']} financial={d['financial_status']} "
                  f"total={d['total']} refunded={d['refunded_amount']} lines={len(d['lines'])} "
                  f"payments={len(d['payments'])} fulfillments={len(d['fulfillments'])} refunds={len(d['refunds'])}")
        else:
            print(f"\nGET /orders/{order_id} -> {detail.status_code} {detail.text[:200]}")

    if db:
        after = counts(db)
        print("\nDatabricks row changes")
        for table, n in after.items():
            print(f"  {table:<20}{before.get(table, 0):>8,} -> {n:>8,}  ({n - before.get(table, 0):+d})")
        db.close()


if __name__ == "__main__":
    main()
