-- Commerce POC — Unity Catalog DDL (Delta).
--
-- Runnable as-is in the Databricks SQL editor. It uses the default names
-- commerce_poc / core / raw / metrics; scripts/bootstrap_databricks.py rewrites them from
-- DATABRICKS_CATALOG / DATABRICKS_SCHEMA_CORE / DATABRICKS_SCHEMA_RAW / DATABRICKS_SCHEMA_METRICS.
-- The metric views themselves live in sql/metric_views/*.yaml (see app/metrics.py).
--
-- PRIMARY KEY constraints are informational in Unity Catalog (not enforced).
-- Foreign keys are documented in column comments ("FK -> table.column") and enforced
-- by the seed generator and the webhook MERGE code, not by Databricks.
-- Money is DECIMAL(18,2); timestamps are UTC.

CREATE CATALOG IF NOT EXISTS commerce_poc;

CREATE SCHEMA IF NOT EXISTS commerce_poc.raw
  COMMENT 'Commerce POC: immutable webhook landing / event log'
  WITH DBPROPERTIES ('commerce_poc.managed' = 'true');

CREATE SCHEMA IF NOT EXISTS commerce_poc.core
  COMMENT 'Commerce POC: current-state commerce tables read by the query API'
  WITH DBPROPERTIES ('commerce_poc.managed' = 'true');

CREATE SCHEMA IF NOT EXISTS commerce_poc.metrics
  COMMENT 'Commerce POC: Unity Catalog metric views (governed KPIs) over core'
  WITH DBPROPERTIES ('commerce_poc.managed' = 'true');

-- ---------------------------------------------------------------- customers
CREATE TABLE IF NOT EXISTS commerce_poc.core.customers (
  customer_id          STRING NOT NULL COMMENT 'PK, cust_<hash>',
  provider             STRING          COMMENT 'Source system, e.g. shopify',
  provider_customer_id STRING          COMMENT 'gid://shopify/Customer/<id>',
  email                STRING,
  first_name           STRING,
  last_name            STRING,
  phone                STRING,
  region               STRING          COMMENT 'West | East | North | South',
  country              STRING          COMMENT 'ISO-3166 alpha-2',
  segment              STRING          COMMENT 'Consumer | SMB | Enterprise',
  marketing_opt_in     BOOLEAN,
  created_at           TIMESTAMP,
  updated_at           TIMESTAMP,
  status               STRING          COMMENT 'active | disabled',
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
) USING DELTA
COMMENT 'Customers. Unique on customer_id and on (provider, provider_customer_id).';

-- ---------------------------------------------------------------- addresses
CREATE TABLE IF NOT EXISTS commerce_poc.core.addresses (
  address_id  STRING NOT NULL COMMENT 'PK, addr_<hash>',
  customer_id STRING          COMMENT 'FK -> customers.customer_id',
  type        STRING          COMMENT 'billing | shipping',
  line1       STRING,
  city        STRING,
  state       STRING,
  postal_code STRING,
  country     STRING,
  is_default  BOOLEAN,
  CONSTRAINT addresses_pk PRIMARY KEY (address_id)
) USING DELTA
COMMENT 'Customer billing/shipping addresses.';

-- ---------------------------------------------------------------- items
CREATE TABLE IF NOT EXISTS commerce_poc.core.items (
  item_id             STRING NOT NULL COMMENT 'PK, item_<hash>',
  provider            STRING,
  provider_product_id STRING          COMMENT 'gid://shopify/Product/<id>',
  provider_variant_id STRING          COMMENT 'Default variant gid',
  sku                 STRING          COMMENT 'Default variant SKU',
  name                STRING,
  description         STRING,
  category            STRING          COMMENT 'Hair Care | Skin Care | Bath & Body | Fragrance | Home Care',
  brand               STRING,
  status              STRING          COMMENT 'active | archived | draft',
  created_at          TIMESTAMP,
  updated_at          TIMESTAMP,
  CONSTRAINT items_pk PRIMARY KEY (item_id)
) USING DELTA
COMMENT 'Products. Unique on (provider, provider_product_id).';

-- ---------------------------------------------------------------- item_variants
CREATE TABLE IF NOT EXISTS commerce_poc.core.item_variants (
  variant_id          STRING NOT NULL COMMENT 'PK, var_<hash>',
  item_id             STRING          COMMENT 'FK -> items.item_id',
  provider_variant_id STRING          COMMENT 'gid://shopify/ProductVariant/<id>',
  sku                 STRING,
  title               STRING          COMMENT 'e.g. 250 ml',
  price               DECIMAL(18,2),
  currency            STRING,
  cost                DECIMAL(18,2),
  inventory_quantity  INT,
  weight_grams        INT,
  barcode             STRING,
  status              STRING          COMMENT 'active | archived',
  CONSTRAINT item_variants_pk PRIMARY KEY (variant_id)
) USING DELTA
COMMENT 'Sellable SKUs (size variants) of an item.';

-- ---------------------------------------------------------------- orders
CREATE TABLE IF NOT EXISTS commerce_poc.core.orders (
  order_id            STRING NOT NULL COMMENT 'PK, ord_<hash>',
  provider            STRING,
  provider_order_id   STRING          COMMENT 'gid://shopify/Order/<id>',
  customer_id         STRING          COMMENT 'FK -> customers.customer_id (NULL for guest checkout)',
  order_number        STRING,
  status              STRING          COMMENT 'pending | paid | fulfilled | partially_fulfilled | cancelled | refunded | partially_refunded',
  financial_status    STRING          COMMENT 'pending | authorized | paid | partially_refunded | refunded | voided',
  fulfillment_status  STRING          COMMENT 'unfulfilled | partial | fulfilled',
  currency            STRING,
  subtotal            DECIMAL(18,2)   COMMENT 'sum(quantity * unit_price)',
  tax                 DECIMAL(18,2)   COMMENT 'sum(line_tax)',
  shipping            DECIMAL(18,2),
  discount            DECIMAL(18,2)   COMMENT 'sum(line_discount)',
  total               DECIMAL(18,2)   COMMENT 'subtotal - discount + tax + shipping',
  source              STRING          COMMENT 'online | pos | draft',
  ordered_at          TIMESTAMP,
  cancelled_at        TIMESTAMP,
  updated_at          TIMESTAMP,
  shipping_address_id STRING          COMMENT 'FK -> addresses.address_id (nullable)',
  billing_address_id  STRING          COMMENT 'FK -> addresses.address_id (nullable)',
  CONSTRAINT orders_pk PRIMARY KEY (order_id)
) USING DELTA
CLUSTER BY (ordered_at)
COMMENT 'Order headers. Unique on (provider, provider_order_id).';

-- ---------------------------------------------------------------- order_details
CREATE TABLE IF NOT EXISTS commerce_poc.core.order_details (
  order_detail_id    STRING NOT NULL COMMENT 'PK, od_<hash>',
  order_id           STRING          COMMENT 'FK -> orders.order_id',
  item_id            STRING          COMMENT 'FK -> items.item_id',
  variant_id         STRING          COMMENT 'FK -> item_variants.variant_id',
  sku                STRING,
  title              STRING,
  quantity           INT,
  unit_price         DECIMAL(18,2),
  line_discount      DECIMAL(18,2),
  line_tax           DECIMAL(18,2),
  line_total         DECIMAL(18,2)   COMMENT 'quantity * unit_price - line_discount + line_tax',
  fulfillment_status STRING          COMMENT 'unfulfilled | fulfilled',
  CONSTRAINT order_details_pk PRIMARY KEY (order_detail_id)
) USING DELTA
CLUSTER BY (order_id)
COMMENT 'Order line items.';

-- ---------------------------------------------------------------- payments
CREATE TABLE IF NOT EXISTS commerce_poc.core.payments (
  payment_id          STRING NOT NULL COMMENT 'PK, pay_<hash>',
  order_id            STRING          COMMENT 'FK -> orders.order_id',
  provider            STRING,
  provider_payment_id STRING          COMMENT 'gid://shopify/OrderTransaction/<id>',
  method              STRING          COMMENT 'card | shopify_payments | paypal | gift_card | other',
  amount              DECIMAL(18,2),
  currency            STRING,
  status              STRING          COMMENT 'authorized | captured | failed | voided',
  processed_at        TIMESTAMP,
  CONSTRAINT payments_pk PRIMARY KEY (payment_id)
) USING DELTA
COMMENT 'Payment transactions against an order.';

-- ---------------------------------------------------------------- fulfillments
CREATE TABLE IF NOT EXISTS commerce_poc.core.fulfillments (
  fulfillment_id   STRING NOT NULL COMMENT 'PK, ful_<hash>',
  order_id         STRING          COMMENT 'FK -> orders.order_id',
  status           STRING          COMMENT 'pending | success | cancelled | error',
  tracking_company STRING,
  tracking_number  STRING,
  shipped_at       TIMESTAMP,
  delivered_at     TIMESTAMP,
  CONSTRAINT fulfillments_pk PRIMARY KEY (fulfillment_id)
) USING DELTA
COMMENT 'Shipments for an order.';

-- ---------------------------------------------------------------- refunds
CREATE TABLE IF NOT EXISTS commerce_poc.core.refunds (
  refund_id          STRING NOT NULL COMMENT 'PK, ref_<hash>',
  order_id           STRING          COMMENT 'FK -> orders.order_id',
  provider           STRING,
  provider_refund_id STRING          COMMENT 'gid://shopify/Refund/<id>',
  amount             DECIMAL(18,2),
  currency           STRING,
  reason             STRING          COMMENT 'customer | inventory | other',
  status             STRING          COMMENT 'pending | succeeded | failed',
  refunded_at        TIMESTAMP,
  note               STRING,
  CONSTRAINT refunds_pk PRIMARY KEY (refund_id)
) USING DELTA
COMMENT 'Refunds. Unique on (provider, provider_refund_id).';

-- ---------------------------------------------------------------- refund_details
CREATE TABLE IF NOT EXISTS commerce_poc.core.refund_details (
  refund_detail_id STRING NOT NULL COMMENT 'PK, rd_<hash>',
  refund_id        STRING          COMMENT 'FK -> refunds.refund_id',
  order_detail_id  STRING          COMMENT 'FK -> order_details.order_detail_id',
  quantity         INT,
  amount           DECIMAL(18,2),
  restock          BOOLEAN,
  CONSTRAINT refund_details_pk PRIMARY KEY (refund_detail_id)
) USING DELTA
COMMENT 'Refunded line items.';

-- ---------------------------------------------------------------- inventory_movements
CREATE TABLE IF NOT EXISTS commerce_poc.core.inventory_movements (
  movement_id    STRING NOT NULL COMMENT 'PK, mov_<hash>',
  variant_id     STRING          COMMENT 'FK -> item_variants.variant_id',
  order_id       STRING          COMMENT 'FK -> orders.order_id (nullable)',
  quantity_delta INT             COMMENT 'positive = stock in',
  reason         STRING          COMMENT 'receipt | sale | restock | adjustment',
  occurred_at    TIMESTAMP,
  CONSTRAINT inventory_movements_pk PRIMARY KEY (movement_id)
) USING DELTA
COMMENT 'Stock ledger. sum(quantity_delta) per variant = item_variants.inventory_quantity for seed data.';

-- ---------------------------------------------------------------- fx_rates
CREATE TABLE IF NOT EXISTS commerce_poc.core.fx_rates (
  currency    STRING NOT NULL COMMENT 'PK, ISO-4217',
  rate_to_usd DECIMAL(18,6)   COMMENT 'USD per 1 unit of currency',
  updated_at  TIMESTAMP,
  CONSTRAINT fx_rates_pk PRIMARY KEY (currency)
) USING DELTA
COMMENT 'Reference FX rates used by the metric views to report *_usd measures. Static illustrative rates (POC).';

MERGE INTO commerce_poc.core.fx_rates t
USING (
  SELECT * FROM VALUES
    ('USD', CAST(1.000000 AS DECIMAL(18,6))),
    ('CAD', CAST(0.720000 AS DECIMAL(18,6))),
    ('GBP', CAST(1.270000 AS DECIMAL(18,6))),
    ('EUR', CAST(1.080000 AS DECIMAL(18,6))),
    ('AUD', CAST(0.660000 AS DECIMAL(18,6)))
  AS v(currency, rate_to_usd)
) s ON t.currency = s.currency
WHEN NOT MATCHED THEN INSERT (currency, rate_to_usd, updated_at) VALUES (s.currency, s.rate_to_usd, current_timestamp());

-- ---------------------------------------------------------------- raw.webhook_events
CREATE TABLE IF NOT EXISTS commerce_poc.raw.webhook_events (
  event_id          STRING NOT NULL COMMENT 'PK, X-Shopify-Webhook-Id or generated',
  provider          STRING          COMMENT 'shopify',
  topic             STRING          COMMENT 'orders/create, refunds/create, ...',
  shop_domain       STRING,
  hmac_valid        BOOLEAN,
  payload_json      STRING,
  processing_status STRING          COMMENT 'received | applied | duplicate | skipped | failed',
  error_message     STRING,
  received_at       TIMESTAMP,
  processed_at      TIMESTAMP,
  CONSTRAINT webhook_events_pk PRIMARY KEY (event_id)
) USING DELTA
CLUSTER BY (received_at)
COMMENT 'Append-only webhook landing table. Rows are only updated to record processing outcome.';

-- ---------------------------------------------------------------- views
CREATE OR REPLACE VIEW commerce_poc.core.v_order_summary
COMMENT 'Order header + customer email + item count + refunded amount'
AS
SELECT
  o.order_id,
  o.order_number,
  o.provider_order_id,
  o.customer_id,
  c.email                              AS customer_email,
  o.status,
  o.financial_status,
  o.fulfillment_status,
  o.currency,
  o.subtotal,
  o.discount,
  o.tax,
  o.shipping,
  o.total,
  o.source                             AS channel,
  o.ordered_at,
  COALESCE(d.item_count, 0)            AS item_count,
  COALESCE(r.refunded_amount, 0)       AS refunded_amount
FROM commerce_poc.core.orders o
LEFT JOIN commerce_poc.core.customers c ON c.customer_id = o.customer_id
LEFT JOIN (
  SELECT order_id, SUM(quantity) AS item_count
  FROM commerce_poc.core.order_details GROUP BY order_id
) d ON d.order_id = o.order_id
LEFT JOIN (
  SELECT order_id, SUM(amount) AS refunded_amount
  FROM commerce_poc.core.refunds WHERE status = 'succeeded' GROUP BY order_id
) r ON r.order_id = o.order_id;

CREATE OR REPLACE VIEW commerce_poc.core.v_customer_ltv
COMMENT 'Customer lifetime value. Gross excludes cancelled orders, refunds are succeeded refunds only. net is in mixed order currencies, net_usd converts via fx_rates.'
AS
SELECT
  c.customer_id,
  c.email,
  c.first_name,
  c.last_name,
  c.region,
  c.segment,
  c.country,
  COALESCE(o.order_count, 0)                                AS order_count,
  CAST(COALESCE(o.gross, 0) AS DECIMAL(18,2))               AS gross,
  CAST(COALESCE(r.refunds, 0) AS DECIMAL(18,2))             AS refunds,
  CAST(COALESCE(o.gross, 0) - COALESCE(r.refunds, 0) AS DECIMAL(18,2)) AS net,
  CAST(COALESCE(o.gross_usd, 0) - COALESCE(r.refunds_usd, 0) AS DECIMAL(18,2)) AS net_usd,
  o.first_order_at,
  o.last_order_at
FROM commerce_poc.core.customers c
LEFT JOIN (
  SELECT ord.customer_id, COUNT(*) AS order_count, SUM(ord.total) AS gross,
         SUM(ord.total * fx.rate_to_usd) AS gross_usd,
         MIN(ord.ordered_at) AS first_order_at, MAX(ord.ordered_at) AS last_order_at
  FROM commerce_poc.core.orders ord
  LEFT JOIN commerce_poc.core.fx_rates fx ON fx.currency = ord.currency
  WHERE ord.status <> 'cancelled' GROUP BY ord.customer_id
) o ON o.customer_id = c.customer_id
LEFT JOIN (
  SELECT ord.customer_id, SUM(rf.amount) AS refunds, SUM(rf.amount * fx.rate_to_usd) AS refunds_usd
  FROM commerce_poc.core.refunds rf
  JOIN commerce_poc.core.orders ord ON ord.order_id = rf.order_id
  LEFT JOIN commerce_poc.core.fx_rates fx ON fx.currency = rf.currency
  WHERE rf.status = 'succeeded' GROUP BY ord.customer_id
) r ON r.customer_id = c.customer_id;
