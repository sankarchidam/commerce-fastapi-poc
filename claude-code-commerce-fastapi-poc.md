# Claude Code prompt — Commerce FastAPI + Databricks POC

Paste everything below the line into a new Claude Code session (preferably in a new folder, or a sibling of this repo).

---

# Task
Build a working Python FastAPI proof-of-concept for a Commerce domain on a private Databricks account.

Do not stop at a design doc. Produce runnable code, DDL, seed data, webhook ingestion, FastAPI query APIs, README, and a local smoke-test path.

This is a new project (or a new top-level folder if you are already inside a repo). Keep it self-contained. Do not depend on dbt or MetricFlow.

# Goal
1. Create a Unity Catalog schema for Commerce.
2. Model Customers, Items, Orders, Refunds, Order Details, plus the extra entities a real commerce domain needs.
3. Load realistic seed data at demo scale: at least 500 customers, 200 items, 5,000 orders, corresponding order details, payments, fulfillments, and refunds.
4. Expose FastAPI endpoints that query that data from Databricks.
5. Accept inbound provider webhooks (Shopify-style) that upsert operational data into Databricks.

# Domain flavor
Use a **personal-care / household shop** catalog so the data looks like a real storefront, not generic "Item 1" rows. Categories should include Hair Care, Skin Care, Bath & Body, and Fragrance. Product names should be realistic (e.g. Lavender Shampoo, Argan Skin Oil). Customers should have real-looking names, emails, regions (West / East / North / South), and segments (Consumer / SMB / Enterprise).

# Non-goals
- Not a production Shopify app, OAuth, or public storefront.
- Not a BI/semantic layer.
- Not a full CDC platform.
- Do not invent a second warehouse. Databricks is the system of record.

# Architecture
Treat Databricks as the lakehouse of record. FastAPI is a thin API + webhook ingress layer.

Use two Unity Catalog schemas in one catalog:

- `{catalog}.raw` — immutable webhook landing / event log
- `{catalog}.core` — current-state commerce tables used by query APIs

Default catalog name: `commerce_poc` (overridable via env).

Ingestion path:

1. Provider POSTs a webhook to FastAPI.
2. Verify HMAC (Shopify `X-Shopify-Hmac-Sha256` + shared secret).
3. Persist the raw payload to `raw.webhook_events` (idempotent on provider event id).
4. Map the payload into `core.*` tables with upserts (MERGE).
5. Query APIs read only from `core.*` with parameterized SQL. Never concatenate user input into SQL.

Also provide a one-shot bootstrap that:
- creates catalog/schemas/tables
- generates synthetic history
- bulk-loads it into `core.*`
- writes a few sample `raw.webhook_events` so the landing table is not empty

# Databricks connection
Use the Databricks SQL Warehouse connector (`databricks-sql-connector`), not notebooks as the runtime.

Read from environment (support `.env` via python-dotenv):

```
DATABRICKS_HOST=
DATABRICKS_HTTP_PATH=
DATABRICKS_TOKEN=
DATABRICKS_CATALOG=commerce_poc
DATABRICKS_SCHEMA_CORE=core
DATABRICKS_SCHEMA_RAW=raw
SHOPIFY_WEBHOOK_SECRET=dev-webhook-secret
API_KEY=dev-api-key
```

Never commit real tokens. Ship `.env.example` only.

Use a small connection helper with context-manager checkout. Do not open a new warehouse connection per row.

If Databricks is unreachable, fail clearly. Do not silently fall back to SQLite unless `COMMERCE_BACKEND=local` is set. If you add a local backend, it must be optional and use the same Pydantic response models.

# Data model (Unity Catalog / Delta)
Create Delta tables. Use generated surrogate string IDs that look operational (`cust_...`, `ord_...`, `gid://shopify/...` is fine for provider ids). Keep both internal ids and provider ids.

## core.customers
- customer_id (PK)
- provider, provider_customer_id
- email, first_name, last_name, phone
- region, country, segment (Consumer / SMB / Enterprise)
- marketing_opt_in
- created_at, updated_at
- status (active, disabled)

## core.addresses
- address_id (PK), customer_id (FK)
- type (billing, shipping)
- line1, city, state, postal_code, country
- is_default

## core.items  (products / SKUs)
- item_id (PK)
- provider, provider_product_id, provider_variant_id
- sku, name, description
- category, brand
- status (active, archived, draft)
- created_at, updated_at

## core.item_variants
- variant_id (PK), item_id (FK)
- sku, title, price, currency, cost
- inventory_quantity
- weight_grams
- barcode
- status

## core.orders
- order_id (PK)
- provider, provider_order_id
- customer_id (FK)
- order_number
- status: pending, paid, fulfilled, partially_fulfilled, cancelled, refunded, partially_refunded
- financial_status, fulfillment_status
- currency
- subtotal, tax, shipping, discount, total
- source (online, pos, draft)
- ordered_at, cancelled_at, updated_at
- shipping_address_id, billing_address_id (nullable FKs)

## core.order_details  (line items)
- order_detail_id (PK)
- order_id (FK)
- item_id, variant_id
- sku, title
- quantity, unit_price, line_discount, line_tax, line_total
- fulfillment_status

## core.payments
- payment_id (PK), order_id (FK)
- provider, provider_payment_id
- method (card, shopify_payments, paypal, gift_card, other)
- amount, currency, status (authorized, captured, failed, voided)
- processed_at

## core.fulfillments
- fulfillment_id (PK), order_id (FK)
- status (pending, success, cancelled, error)
- tracking_company, tracking_number
- shipped_at, delivered_at

## core.refunds
- refund_id (PK), order_id (FK)
- provider, provider_refund_id
- amount, currency
- reason (customer, inventory, other)
- status (pending, succeeded, failed)
- refunded_at, note

## core.refund_details
- refund_detail_id (PK)
- refund_id (FK), order_detail_id (FK)
- quantity, amount, restock (boolean)

## core.inventory_movements (optional but recommended)
- movement_id, variant_id, order_id nullable, quantity_delta, reason, occurred_at

## raw.webhook_events
- event_id (PK) — Shopify `X-Shopify-Webhook-Id` or generated
- provider (shopify, etc.)
- topic (orders/create, orders/updated, refunds/create, products/update, customers/create, ...)
- shop_domain
- hmac_valid
- payload_json STRING
- processing_status (received, applied, duplicate, failed)
- error_message
- received_at, processed_at

Add useful Delta properties: liquid clustering or simple ZORDER notes in comments are optional. At minimum, define primary-key-like uniqueness and helper views:

- `core.v_order_summary` — order + customer email + item_count + refunded_amount
- `core.v_customer_ltv` — customer + order_count + gross + refunds + net

Include FK-like comments in DDL even if Databricks does not enforce them. Enforce integrity in the generator and MERGE code.

# Seed data
Write `scripts/generate_seed_data.py` + `scripts/load_databricks.py`.

Scale (minimum):
- 500 customers
- 200 items, 1–3 variants each
- 5,000 orders over the last 18 months, dates anchored to *today*
- 1–5 line items per order
- ~90% of orders have a captured payment
- ~70% of paid orders have a fulfillment
- ~8–12% of orders have a refund (full or partial)
- Realistic names, emails, US/CA/UK addresses, personal-care product catalog
- Deterministic `random.seed`

Referential integrity is mandatory. Totals on orders must equal the sum of line items ± tax/shipping/discount.

Loading strategy:
- Generate locally (parquet or CSV in `data/generated/`, gitignored except a tiny sample)
- Load with batched INSERTs or spark-connect/SQL warehouse PUT+COPY if practical
- Prefer pandas → `databricks-sql` executemany in chunks of 500–1000 rows
- Print row counts and a few sanity queries at the end

Also ship `sql/ddl.sql` so tables can be created without Python.

# FastAPI app
Python 3.11+, FastAPI, Pydantic v2, Uvicorn.

Layout:

```
README.md
.env.example
pyproject.toml or requirements.txt
sql/ddl.sql
sql/queries/*.sql   # named queries, not inline spaghetti
app/main.py
app/config.py
app/db.py
app/models.py       # response/request schemas
app/routers/health.py
app/routers/customers.py
app/routers/items.py
app/routers/orders.py
app/routers/refunds.py
app/routers/webhooks.py
app/services/shopify_mapper.py
app/services/ingest.py
scripts/bootstrap_databricks.py
scripts/generate_seed_data.py
scripts/load_databricks.py
scripts/simulate_webhooks.py
tests/...
```

## Query API (protect with `X-API-Key` matching `API_KEY`)
All list endpoints: pagination (`limit` default 50, max 200, `offset` or cursor), and `total` in the response envelope.

- `GET /health` — app up; `GET /health/databricks` — `SELECT 1` plus catalog/schema visibility
- `GET /customers` — filter email, region, segment, q=search
- `GET /customers/{customer_id}` — profile + addresses + order_count
- `GET /items` — filter category, status, q=name/sku
- `GET /items/{item_id}` — item + variants + inventory
- `GET /orders` — filter customer_id, status, financial_status, date_from, date_to, provider_order_id
- `GET /orders/{order_id}` — header + details + payments + fulfillments + refunds
- `GET /refunds` — filter order_id, date range
- `GET /refunds/{refund_id}`
- `GET /webhooks/events` — recent raw events (ops/debug), filter topic/status
- `GET /stats/overview` — counts: customers, items, orders, refunds, GMV, refunded amount, last webhook time

Return JSON that is stable and typed. Use ISO-8601 timestamps. Decimal money as strings or two-decimal floats consistently (prefer Decimal serialized as string).

## Webhook API (HMAC, not API key)
- `POST /webhooks/shopify/{topic}` where topic is path-safe, e.g. `orders_create`, `orders_updated`, `refunds_create`, `products_create`, `products_update`, `customers_create`, `customers_update`
- Also accept `X-Shopify-Topic` header and ignore path if header present
- Verify HMAC-SHA256 of raw body against `SHOPIFY_WEBHOOK_SECRET`
- On invalid HMAC: 401, do not write business tables
- On valid HMAC: 200 quickly after persisting `raw.webhook_events`; apply to `core` in the same request for the POC (document that production would queue)
- Idempotent: duplicate `X-Shopify-Webhook-Id` returns 200 `{ "status": "duplicate" }` and does not double-apply
- Unknown topic: store raw event, skip mapper, 202

Map at least these Shopify-like payloads (subset is fine, document the fields you honor):

- `orders/create` and `orders/updated` → customers (upsert by email/provider id), orders, order_details, payments
- `refunds/create` → refunds + refund_details; update order financial_status
- `products/create` and `products/update` → items + variants
- `customers/create` and `customers/update` → customers + default address

Keep sample JSON fixtures under `tests/fixtures/shopify/`.

# Webhook simulator
`scripts/simulate_webhooks.py` should:
- build a Shopify-like order payload from seed-style fake data
- HMAC-sign it
- POST to local FastAPI
- print Databricks row changes

This is how a reviewer proves the webhook path without a real Shopify store.

# Quality bar
- Type hints, no wildcard imports
- Structured logging (request id, topic, event_id, duration_ms)
- Pydantic settings
- FastAPI OpenAPI tags and examples
- SQL injection safe
- MERGE/upsert logic for provider ids
- Tests with TestClient:
  - HMAC reject
  - HMAC accept + idempotent replay
  - order list pagination
  - order detail shape
- Mock Databricks in unit tests; one optional integration test gated by env

# README must include
1. What the POC is
2. Catalog/schema/table diagram (mermaid)
3. Env setup for a private Databricks account (SQL warehouse host, HTTP path, PAT)
4. `bootstrap` → `generate` → `load` → `uvicorn` commands
5. curl examples for query APIs and a signed webhook
6. How Shopify would be pointed at `/webhooks/shopify/...` (ngrok note is enough)
7. Known POC shortcuts (sync apply, PAT auth, no SCD2, money as DECIMAL(18,2))

# Implementation order
1. Project skeleton + config + db helper + health
2. DDL + bootstrap against Databricks
3. Seed generator + loader; verify counts with SQL
4. Query routers
5. Webhook verify + raw land + mappers + MERGE
6. Simulator + tests + README
7. Run a real smoke test if Databricks env vars are present; otherwise still make unit tests pass

# Constraints
- Prefer boring, explicit Python over frameworks-on-frameworks
- Do not put secrets in source
- Do not require Terraform
- Keep the first successful demo under ~15 minutes after env vars are set
