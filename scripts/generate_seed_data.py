"""Generate deterministic synthetic commerce history for a personal-care / household shop.

Writes one parquet file per core table to data/generated/ and a small CSV sample to
data/sample/. Dates are anchored to today (UTC midnight), so re-running on the same day
produces identical output.

    python -m scripts.generate_seed_data            # default demo scale
    python -m scripts.generate_seed_data --orders 20000
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from faker import Faker

from app.tables import CORE_LOAD_ORDER, column_names, make_id, shopify_gid

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "generated"
SAMPLE_DIR = ROOT / "data" / "sample"
PROVIDER = "shopify"
CENT = Decimal("0.01")

Row = dict[str, Any]


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- catalog
# (product type, base price for the smallest size, size ladder)
CATALOG: dict[str, list[tuple[str, float, list[str]]]] = {
    "Hair Care": [
        ("Shampoo", 9.5, ["250 ml", "500 ml", "1 L"]),
        ("Conditioner", 10.5, ["250 ml", "500 ml", "1 L"]),
        ("Hair Mask", 16.0, ["200 ml", "400 ml"]),
        ("Leave-In Conditioner", 14.0, ["150 ml", "300 ml"]),
        ("Hair Serum", 19.0, ["50 ml", "100 ml"]),
        ("Dry Shampoo", 11.0, ["100 ml", "200 ml"]),
        ("Scalp Scrub", 18.0, ["200 ml"]),
    ],
    "Skin Care": [
        ("Face Cleanser", 14.0, ["150 ml", "300 ml"]),
        ("Skin Oil", 24.0, ["30 ml", "50 ml", "100 ml"]),
        ("Moisturizer", 22.0, ["50 ml", "100 ml"]),
        ("Night Cream", 28.0, ["50 ml"]),
        ("Toner", 15.0, ["200 ml", "400 ml"]),
        ("Face Serum", 32.0, ["30 ml", "50 ml"]),
        ("Clay Mask", 18.0, ["100 ml"]),
        ("Lip Balm", 5.5, ["4.5 g", "3-pack"]),
    ],
    "Bath & Body": [
        ("Body Wash", 10.0, ["300 ml", "500 ml", "1 L"]),
        ("Body Lotion", 13.0, ["250 ml", "500 ml"]),
        ("Bath Bomb", 6.5, ["single", "4-pack", "12-pack"]),
        ("Body Scrub", 17.0, ["200 g", "400 g"]),
        ("Bar Soap", 6.0, ["single", "3-pack", "6-pack"]),
        ("Hand Cream", 9.0, ["75 ml", "150 ml"]),
        ("Bath Salts", 14.0, ["500 g", "1 kg"]),
        ("Body Butter", 19.0, ["200 ml"]),
    ],
    "Fragrance": [
        ("Eau de Parfum", 48.0, ["10 ml", "50 ml", "100 ml"]),
        ("Eau de Toilette", 36.0, ["30 ml", "100 ml"]),
        ("Body Mist", 16.0, ["100 ml", "250 ml"]),
        ("Perfume Oil", 28.0, ["10 ml"]),
        ("Scented Candle", 26.0, ["200 g", "400 g"]),
        ("Reed Diffuser", 32.0, ["100 ml", "200 ml"]),
    ],
    "Home Care": [
        ("Hand Soap Refill", 12.0, ["500 ml", "1 L"]),
        ("Laundry Detergent", 15.0, ["1 L", "2 L"]),
        ("Dish Soap", 6.0, ["500 ml", "1 L"]),
        ("Linen Spray", 13.0, ["250 ml"]),
        ("All-Purpose Cleaner", 8.0, ["500 ml", "1 L"]),
    ],
}
CATEGORY_WEIGHTS = {"Hair Care": 0.25, "Skin Care": 0.25, "Bath & Body": 0.25, "Fragrance": 0.15, "Home Care": 0.10}
CATEGORY_CODE = {"Hair Care": "HC", "Skin Care": "SC", "Bath & Body": "BB", "Fragrance": "FR", "Home Care": "HM"}
SCENTS = [
    "Lavender", "Argan", "Coconut", "Tea Tree", "Rosemary Mint", "Shea", "Charcoal", "Aloe",
    "Citrus", "Vanilla", "Eucalyptus", "Rose", "Oat & Honey", "Sandalwood", "Bergamot",
    "Jasmine", "Cucumber", "Sea Salt", "Green Tea", "Almond", "Neroli", "Cedarwood",
    "Peppermint", "Fig", "Grapefruit", "Chamomile", "Vetiver", "Wild Berry",
]
BRANDS = [
    "Meadow & Moss", "Saltwater Apothecary", "Northfield Naturals", "Ember & Oak",
    "Pure Grove", "Luma Botanics", "Harbor Lane", "Willow Bay",
]
DESCRIPTION_HINTS = {
    "Hair Care": "Sulfate-free formula that cleans gently and leaves hair soft and shiny.",
    "Skin Care": "Lightweight, fast-absorbing and dermatologist tested for everyday use.",
    "Bath & Body": "Rich, skin-loving formula with plant oils for a calm, spa-like ritual.",
    "Fragrance": "A long-lasting blend layered over warm, soft base notes.",
    "Home Care": "Plant-based, biodegradable and safe on everyday surfaces.",
}

COUNTRIES = [("US", 0.70, "en_US", "USD", Decimal("0.07")),
             ("CA", 0.15, "en_CA", "CAD", Decimal("0.13")),
             ("GB", 0.15, "en_GB", "GBP", Decimal("0.20"))]
REGIONS = ["West", "East", "North", "South"]
SEGMENTS = [("Consumer", 0.75), ("SMB", 0.18), ("Enterprise", 0.07)]
CARRIERS = {"US": ["UPS", "USPS", "FedEx"], "CA": ["Canada Post", "Purolator"], "GB": ["Royal Mail", "DPD"]}
EMAIL_DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me", "hotmail.com"]


@dataclass
class Dataset:
    tables: dict[str, list[Row]] = field(default_factory=lambda: {t: [] for t in CORE_LOAD_ORDER})

    def add(self, table: str, row: Row) -> None:
        self.tables[table].append(row)

    def counts(self) -> dict[str, int]:
        return {t: len(rows) for t, rows in self.tables.items()}


def _weighted(rng: random.Random, pairs: list[tuple[Any, float]]) -> Any:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs], k=1)[0]


def _ts(rng: random.Random, start: datetime, end: datetime) -> datetime:
    span = max(int((end - start).total_seconds()), 1)
    return (start + timedelta(seconds=rng.randrange(span))).replace(microsecond=0)


def generate(
    customers: int = 500,
    items: int = 200,
    orders: int = 5000,
    seed: int = 42,
    today: datetime | None = None,
) -> Dataset:
    rng = random.Random(seed)
    fakers = {code: Faker(locale) for code, _, locale, _, _ in COUNTRIES}
    for i, f in enumerate(fakers.values()):
        f.seed_instance(seed + i)

    now = (today or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = now - timedelta(days=548)  # ~18 months
    ds = Dataset()

    # ------------------------------------------------------------- customers + addresses
    customer_rows: list[Row] = []
    country_info = {c[0]: c for c in COUNTRIES}
    used_emails: set[str] = set()
    for n in range(customers):
        country = _weighted(rng, [(c[0], c[1]) for c in COUNTRIES])
        fk = fakers[country]
        first, last = fk.first_name(), fk.last_name()
        base = f"{first}.{last}".lower().replace(" ", "").replace("'", "")
        email = f"{base}@{rng.choice(EMAIL_DOMAINS)}"
        while email in used_emails:
            email = f"{base}{rng.randint(2, 999)}@{rng.choice(EMAIL_DOMAINS)}"
        used_emails.add(email)
        pid = shopify_gid("Customer", 7_100_000_000_000 + n)
        created = _ts(rng, now - timedelta(days=730), now - timedelta(days=30))
        row: Row = {
            "customer_id": make_id("cust", PROVIDER, pid),
            "provider": PROVIDER,
            "provider_customer_id": pid,
            "email": email,
            "first_name": first,
            "last_name": last,
            "phone": _phone(rng, country),
            "region": rng.choice(REGIONS),
            "country": country,
            "segment": _weighted(rng, SEGMENTS),
            "marketing_opt_in": rng.random() < 0.55,
            "created_at": created,
            "updated_at": _ts(rng, created, now),
            "status": "active" if rng.random() < 0.97 else "disabled",
        }
        ds.add("customers", row)
        customer_rows.append(row)

        street, city, state, postcode = fk.street_address(), fk.city(), _state(fk, country), fk.postcode()
        for addr_type in ("shipping", "billing"):
            if addr_type == "billing" and rng.random() < 0.3:
                street, city, state, postcode = fk.street_address(), fk.city(), _state(fk, country), fk.postcode()
            ds.add("addresses", {
                "address_id": make_id("addr", row["customer_id"], addr_type),
                "customer_id": row["customer_id"],
                "type": addr_type,
                "line1": street,
                "city": city,
                "state": state,
                "postal_code": postcode,
                "country": country,
                "is_default": True,
            })

    # ------------------------------------------------------------- items + variants
    names_used: set[str] = set()
    sellable: list[tuple[Row, Row]] = []  # (item, variant) for active items
    variant_rows: list[Row] = []
    item_seq = 0
    while item_seq < items:
        category = _weighted(rng, list(CATEGORY_WEIGHTS.items()))
        ptype, base_price, sizes = rng.choice(CATALOG[category])
        name = f"{rng.choice(SCENTS)} {ptype}"
        if name in names_used:
            continue
        names_used.add(name)
        item_seq += 1
        product_gid = shopify_gid("Product", 8_200_000_000 + item_seq)
        item_id = make_id("item", PROVIDER, product_gid)
        status = _weighted(rng, [("active", 0.90), ("archived", 0.06), ("draft", 0.04)])
        created = _ts(rng, now - timedelta(days=900), history_start)
        n_variants = min(len(sizes), rng.choice([1, 2, 2, 3, 3]))
        brand = rng.choice(BRANDS)
        price_jitter = rng.uniform(0.85, 1.25)
        item_variants: list[Row] = []
        for v_idx, size in enumerate(sizes[:n_variants]):
            variant_gid = shopify_gid("ProductVariant", 4_400_000_000 + item_seq * 10 + v_idx)
            price = money(round(base_price * price_jitter * (1 + 0.75 * v_idx)) - 0.01)
            item_variants.append({
                "variant_id": make_id("var", PROVIDER, variant_gid),
                "item_id": item_id,
                "provider_variant_id": variant_gid,
                "sku": f"{CATEGORY_CODE[category]}-{item_seq:04d}-{v_idx + 1}",
                "title": size,
                "price": price,
                "currency": "USD",
                "cost": money(float(price) * rng.uniform(0.32, 0.55)),
                "inventory_quantity": 0,  # filled in from the movement ledger below
                "weight_grams": int(_grams(size) * 1.1) + 40,
                "barcode": f"{rng.randrange(10**11, 10**12)}",
                "status": "active" if status == "active" else "archived",
            })
        ds.add("items", {
            "item_id": item_id,
            "provider": PROVIDER,
            "provider_product_id": product_gid,
            "provider_variant_id": item_variants[0]["provider_variant_id"],
            "sku": item_variants[0]["sku"],
            "name": name,
            "description": f"{name} by {brand}. {DESCRIPTION_HINTS[category]}",
            "category": category,
            "brand": brand,
            "status": status,
            "created_at": created,
            "updated_at": _ts(rng, created, now),
        })
        for v in item_variants:
            ds.add("item_variants", v)
            variant_rows.append(v)
            if status == "active":
                sellable.append((ds.tables["items"][-1], v))

    # Popularity skew: a few hero products sell a lot.
    popularity = [rng.paretovariate(1.3) for _ in sellable]

    # ------------------------------------------------------------- orders
    order_specs = []
    for _ in range(orders):
        cust = rng.choice(customer_rows)
        start = max(cust["created_at"], history_start)
        order_specs.append((_ts(rng, start, now - timedelta(minutes=30)), cust))
    order_specs.sort(key=lambda s: s[0])

    sold = defaultdict(int)
    restocked = defaultdict(int)
    for seq, (ordered_at, cust) in enumerate(order_specs):
        _make_order(ds, rng, seq, ordered_at, cust, sellable, popularity, country_info, now, sold, restocked)

    # ------------------------------------------------------------- inventory ledger
    for v in variant_rows:
        on_hand = 0 if rng.random() < 0.05 else rng.randint(5, 400)
        received = on_hand + sold[v["variant_id"]] - restocked[v["variant_id"]]
        v["inventory_quantity"] = on_hand
        ds.add("inventory_movements", {
            "movement_id": make_id("mov", v["variant_id"], "receipt"),
            "variant_id": v["variant_id"],
            "order_id": None,
            "quantity_delta": received,
            "reason": "receipt",
            "occurred_at": history_start - timedelta(days=rng.randint(1, 60)),
        })

    return ds


def _make_order(
    ds: Dataset,
    rng: random.Random,
    seq: int,
    ordered_at: datetime,
    cust: Row,
    sellable: list[tuple[Row, Row]],
    popularity: list[float],
    country_info: dict[str, tuple],
    now: datetime,
    sold: dict[str, int],
    restocked: dict[str, int],
) -> None:
    country = cust["country"]
    _, _, _, currency, tax_rate = country_info[country]
    order_gid = shopify_gid("Order", 5_600_000_000 + seq)
    order_id = make_id("ord", PROVIDER, order_gid)

    # ---- lines
    n_lines = rng.choices([1, 2, 3, 4, 5], weights=[38, 30, 18, 9, 5], k=1)[0]
    picks: dict[str, tuple[Row, Row]] = {}
    while len(picks) < n_lines:
        item, variant = rng.choices(sellable, weights=popularity, k=1)[0]
        picks[variant["variant_id"]] = (item, variant)
    promo = rng.random() < 0.15
    lines: list[Row] = []
    for idx, (item, variant) in enumerate(picks.values()):
        qty = rng.choices([1, 2, 3], weights=[75, 20, 5], k=1)[0]
        gross = variant["price"] * qty
        disc = money(gross * Decimal("0.10")) if promo else money(0)
        tax = money((gross - disc) * tax_rate)
        lines.append({
            "order_detail_id": make_id("od", PROVIDER, shopify_gid("LineItem", f"{5_600_000_000 + seq}{idx:02d}")),
            "order_id": order_id,
            "item_id": item["item_id"],
            "variant_id": variant["variant_id"],
            "sku": variant["sku"],
            "title": f"{item['name']} - {variant['title']}",
            "quantity": qty,
            "unit_price": variant["price"],
            "line_discount": disc,
            "line_tax": tax,
            "line_total": money(gross - disc + tax),
            "fulfillment_status": "unfulfilled",
        })

    subtotal = money(sum((l["unit_price"] * l["quantity"] for l in lines), Decimal(0)))
    discount = money(sum((l["line_discount"] for l in lines), Decimal(0)))
    tax = money(sum((l["line_tax"] for l in lines), Decimal(0)))
    shipping = money(0) if subtotal - discount >= 50 else money("5.99")
    total = money(subtotal - discount + tax + shipping)

    # ---- lifecycle
    age_days = (now - ordered_at).total_seconds() / 86400
    roll = rng.random()
    if roll < 0.04:
        outcome = "pending"
    elif roll < 0.08:
        outcome = "cancelled"
    elif roll < 0.10:
        outcome = "payment_failed"
    else:
        outcome = "paid"

    status = financial = "pending"
    fulfillment_status = "unfulfilled"
    cancelled_at = None
    updated_at = ordered_at + timedelta(minutes=rng.randint(1, 20))
    method = _weighted(rng, [("shopify_payments", 0.55), ("card", 0.15), ("paypal", 0.22), ("gift_card", 0.05), ("other", 0.03)])
    pay_status = "authorized"
    if outcome == "cancelled":
        status, financial = "cancelled", "voided"
        cancelled_at = min(ordered_at + timedelta(hours=rng.randint(1, 30)), now)
        updated_at = cancelled_at
        pay_status = "voided"
    elif outcome == "payment_failed":
        pay_status = "failed"
    elif outcome == "paid":
        status, financial = "paid", "paid"
        pay_status = "captured"
    elif outcome == "pending":
        financial = "authorized" if rng.random() < 0.5 else "pending"
        pay_status = "authorized" if financial == "authorized" else None

    if pay_status:
        ds.add("payments", {
            "payment_id": make_id("pay", PROVIDER, order_gid, "1"),
            "order_id": order_id,
            "provider": PROVIDER,
            "provider_payment_id": shopify_gid("OrderTransaction", 9_100_000_000 + seq),
            "method": method,
            "amount": total,
            "currency": currency,
            "status": pay_status,
            "processed_at": ordered_at + timedelta(seconds=rng.randint(5, 600)),
        })

    # ---- fulfillment (~70% of paid orders; recent orders are less likely shipped)
    shipped_at = None
    fulfill_p = 0.74 if age_days > 4 else 0.25
    candidate_ship = ordered_at + timedelta(hours=rng.randint(6, 72))
    if outcome == "paid" and candidate_ship < now and rng.random() < fulfill_p:
        shipped_at = candidate_ship
        delivered_at = shipped_at + timedelta(days=rng.randint(2, 7))
        if delivered_at > now:
            delivered_at = None
        partial = len(lines) > 1 and rng.random() < 0.06
        shipped_lines = lines[:-1] if partial else lines
        for l in shipped_lines:
            l["fulfillment_status"] = "fulfilled"
            sold[l["variant_id"]] += l["quantity"]
            ds.add("inventory_movements", {
                "movement_id": make_id("mov", l["order_detail_id"], "sale"),
                "variant_id": l["variant_id"],
                "order_id": order_id,
                "quantity_delta": -l["quantity"],
                "reason": "sale",
                "occurred_at": shipped_at,
            })
        fulfillment_status = "partial" if partial else "fulfilled"
        status = "partially_fulfilled" if partial else "fulfilled"
        carrier = rng.choice(CARRIERS[country])
        ds.add("fulfillments", {
            "fulfillment_id": make_id("ful", PROVIDER, order_gid, "1"),
            "order_id": order_id,
            "status": "success",
            "tracking_company": carrier,
            "tracking_number": _tracking(rng, carrier),
            "shipped_at": shipped_at,
            "delivered_at": delivered_at,
        })
        updated_at = shipped_at

    # ---- refunds (~10% of all orders => ~13% of fulfilled ones)
    if shipped_at and rng.random() < 0.15:
        refunded_at = shipped_at + timedelta(days=rng.randint(3, 25))
        if refunded_at > now:
            # Recent shipment: refund somewhere between shipping and now.
            refunded_at = shipped_at + (now - shipped_at) * rng.uniform(0.2, 0.9)
        refunded_at = refunded_at.replace(microsecond=0)
        full = rng.random() < 0.45
        reason = _weighted(rng, [("customer", 0.70), ("inventory", 0.15), ("other", 0.15)])
        refund_gid = shopify_gid("Refund", 3_300_000_000 + seq)
        refund_id = make_id("ref", PROVIDER, refund_gid)
        refund_status = "succeeded" if rng.random() < 0.96 else "pending"
        fulfilled_lines = [l for l in lines if l["fulfillment_status"] == "fulfilled"]
        targets = fulfilled_lines if full else [rng.choice(fulfilled_lines)]
        amount = Decimal(0)
        for l in targets:
            qty = l["quantity"] if full else rng.randint(1, l["quantity"])
            line_amount = money(l["line_total"] * qty / l["quantity"])
            restock = reason == "customer" and rng.random() < 0.6
            amount += line_amount
            ds.add("refund_details", {
                "refund_detail_id": make_id("rd", refund_id, l["order_detail_id"]),
                "refund_id": refund_id,
                "order_detail_id": l["order_detail_id"],
                "quantity": qty,
                "amount": line_amount,
                "restock": restock,
            })
            if restock:
                restocked[l["variant_id"]] += qty
                ds.add("inventory_movements", {
                    "movement_id": make_id("mov", l["order_detail_id"], "restock"),
                    "variant_id": l["variant_id"],
                    "order_id": order_id,
                    "quantity_delta": qty,
                    "reason": "restock",
                    "occurred_at": refunded_at,
                })
        amount = money(amount)
        ds.add("refunds", {
            "refund_id": refund_id,
            "order_id": order_id,
            "provider": PROVIDER,
            "provider_refund_id": refund_gid,
            "amount": amount,
            "currency": currency,
            "reason": reason,
            "status": refund_status,
            "refunded_at": refunded_at,
            "note": _refund_note(rng, reason),
        })
        if refund_status == "succeeded":
            is_full = amount >= total - shipping
            status = financial = "refunded" if is_full else "partially_refunded"
            updated_at = refunded_at

    addresses = {"shipping": make_id("addr", cust["customer_id"], "shipping"),
                 "billing": make_id("addr", cust["customer_id"], "billing")}
    ds.add("orders", {
        "order_id": order_id,
        "provider": PROVIDER,
        "provider_order_id": order_gid,
        "customer_id": cust["customer_id"],
        "order_number": f"#{1001 + seq}",
        "status": status,
        "financial_status": financial,
        "fulfillment_status": fulfillment_status,
        "currency": currency,
        "subtotal": subtotal,
        "tax": tax,
        "shipping": shipping,
        "discount": discount,
        "total": total,
        "source": _weighted(rng, [("online", 0.85), ("pos", 0.10), ("draft", 0.05)]),
        "ordered_at": ordered_at,
        "cancelled_at": cancelled_at,
        "updated_at": updated_at,
        "shipping_address_id": addresses["shipping"],
        "billing_address_id": addresses["billing"],
    })
    for l in lines:
        ds.add("order_details", l)


def _phone(rng: random.Random, country: str) -> str:
    if country == "GB":
        return f"+44 7{rng.randint(100, 999)} {rng.randint(100000, 999999)}"
    return f"+1-{rng.randint(201, 989)}-{rng.randint(200, 999)}-{rng.randint(0, 9999):04d}"


def _state(fk: Faker, country: str) -> str:
    if country == "US":
        return fk.state_abbr(include_territories=False)
    if country == "CA":
        return fk.province_abbr()
    return fk.county()


def _grams(size: str) -> int:
    num = "".join(ch for ch in size.split()[0] if ch.isdigit() or ch == ".")
    if "pack" in size or size == "single":
        return 120 * (int(size.split("-")[0]) if size[0].isdigit() else 1)
    value = float(num or 100)
    return int(value * 1000) if size.endswith(("L", "kg")) and value < 10 else int(value)


def _tracking(rng: random.Random, carrier: str) -> str:
    if carrier == "UPS":
        return "1Z" + "".join(rng.choices("0123456789ABCDEFGHJKLMNPRSTUVWXYZ", k=16))
    if carrier in ("Royal Mail", "Canada Post"):
        return "".join(rng.choices("ABCDEFGHJKLMNPRSTUVWXYZ", k=2)) + f"{rng.randrange(10**8, 10**9)}" + ("GB" if carrier == "Royal Mail" else "CA")
    return f"{rng.randrange(10**11, 10**12)}{rng.randrange(10**9, 10**10)}"


def _refund_note(rng: random.Random, reason: str) -> str:
    notes = {
        "customer": ["Customer changed their mind", "Scent not as expected", "Arrived damaged", "Wrong size ordered", "Allergic reaction"],
        "inventory": ["Out of stock after order", "Damaged in warehouse", "Batch recall"],
        "other": ["Goodwill credit", "Shipping delay", "Duplicate order"],
    }
    return rng.choice(notes[reason])


def write(ds: Dataset, out_dir: Path = OUT_DIR, sample_dir: Path = SAMPLE_DIR) -> None:
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    sample_dir.mkdir(parents=True, exist_ok=True)
    for table, rows in ds.tables.items():
        df = pd.DataFrame(rows, columns=column_names(table))
        for col in df.columns:
            if df[col].map(lambda v: isinstance(v, Decimal)).any():
                df[col] = df[col].map(lambda v: None if v is None else str(v))
        df.to_parquet(out_dir / f"{table}.parquet", index=False)
        df.head(20).to_csv(sample_dir / f"{table}.csv", index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customers", type=int, default=500)
    parser.add_argument("--items", type=int, default=200)
    parser.add_argument("--orders", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    ds = generate(customers=args.customers, items=args.items, orders=args.orders, seed=args.seed)
    write(ds, args.out)
    for table, n in ds.counts().items():
        print(f"{table:<22}{n:>8,}")
    print(f"wrote parquet to {args.out}")


if __name__ == "__main__":
    main()
