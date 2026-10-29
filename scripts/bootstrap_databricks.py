"""One-shot bootstrap: catalog/schemas/tables -> synthetic history -> bulk load -> sample webhooks.

    python -m scripts.bootstrap_databricks              # everything
    python -m scripts.bootstrap_databricks --ddl-only   # just create catalog/schemas/tables/views/metric views
    python -m scripts.bootstrap_databricks --metrics-only  # just (re)create the metric views
    python -m scripts.bootstrap_databricks --recreate   # drop POC tables first (only in POC-owned schemas)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.db import Database, Session
from app.metrics import load_views, render_create
from app.services.ingest import DatabricksIngestBackend, WebhookIngestor
from app.tables import CORE_LOAD_ORDER
from scripts import generate_seed_data, load_databricks
from scripts.common import ROOT, UnsafeSchemaError, assert_safe_schemas, catalog_exists, render_ddl

FIXTURES = ROOT / "tests" / "fixtures" / "shopify"
SAMPLE_EVENTS = [
    ("customers/create", "customers_create.json"),
    ("products/create", "products_create.json"),
    ("orders/create", "orders_create.json"),
    ("orders/updated", "orders_updated.json"),
    ("refunds/create", "refunds_create.json"),
    ("app/uninstalled", "unknown_topic.json"),
]


def create_objects(s: Session, recreate: bool, force: bool) -> None:
    settings = get_settings()
    cat = settings.databricks_catalog
    statements = render_ddl(settings)
    if catalog_exists(s, cat):
        statements = [st for st in statements if not st.upper().startswith("CREATE CATALOG")]
        print(f"catalog {cat} exists")
        assert_safe_schemas(s, settings, force=force)
    else:
        print(f"creating catalog {cat}")

    if recreate:
        for table in reversed(CORE_LOAD_ORDER):
            s.execute(f"DROP TABLE IF EXISTS {settings.core}.`{table}`")
        s.execute(f"DROP TABLE IF EXISTS {settings.core}.`fx_rates`")
        s.execute(f"DROP TABLE IF EXISTS {settings.raw}.`webhook_events`")
        print("dropped existing POC tables")

    for stmt in statements:
        first_line = stmt.splitlines()[0]
        t0 = time.perf_counter()
        try:
            s.execute(stmt)
        except Exception as exc:
            if first_line.upper().startswith("CREATE CATALOG"):
                sys.exit(
                    f"could not create catalog {cat}: {exc}\n"
                    f"Create it in the Databricks UI (Catalog -> +) or set DATABRICKS_CATALOG to one you can write to."
                )
            raise
        print(f"  ok {time.perf_counter() - t0:5.1f}s  {first_line[:90]}")
    create_metric_views(s)


def create_metric_views(s: Session) -> None:
    settings = get_settings()
    print(f"metric views -> {settings.databricks_catalog}.{settings.databricks_schema_metrics}")
    for view in load_views().values():
        t0 = time.perf_counter()
        s.execute(render_create(view, settings))
        print(f"  ok {time.perf_counter() - t0:5.1f}s  {view.name} "
              f"({len(view.dimensions)} dimensions, {len(view.measures)} measures)")


def send_sample_events(db: Database) -> None:
    ingestor = WebhookIngestor(DatabricksIngestBackend(db), db.settings)
    print("\nsample webhook events -> raw.webhook_events (+ applied to core)")
    for topic, fname in SAMPLE_EVENTS:
        body = (FIXTURES / fname).read_bytes()
        result = ingestor.handle(
            provider="shopify", topic=topic, event_id=f"bootstrap-{Path(fname).stem}",
            shop_domain="commerce-poc-demo.myshopify.com", body=body,
        )
        print(f"  {topic:<18} {result.status:<10} {result.applied or ''} {result.error or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ddl-only", action="store_true")
    parser.add_argument("--metrics-only", action="store_true", help="only (re)create the metric views")
    parser.add_argument("--recreate", action="store_true", help="drop and recreate POC tables")
    parser.add_argument("--force", action="store_true", help="use pre-existing schemas this POC did not create")
    parser.add_argument("--orders", type=int, default=5000)
    args = parser.parse_args()

    settings = get_settings()
    print(f"Databricks {settings.databricks_host}  catalog={settings.databricks_catalog} "
          f"core={settings.databricks_schema_core} raw={settings.databricks_schema_raw} "
          f"metrics={settings.databricks_schema_metrics}")
    db = Database(settings)
    started = time.perf_counter()
    if args.metrics_only:
        with db.session() as s:
            create_metric_views(s)
        db.close()
        return
    try:
        with db.session() as s:
            create_objects(s, args.recreate, args.force)
    except UnsafeSchemaError as exc:
        sys.exit(str(exc))
    if args.ddl_only:
        return

    print("\ngenerating seed data")
    ds = generate_seed_data.generate(orders=args.orders)
    generate_seed_data.write(ds)
    for table, n in ds.counts().items():
        print(f"  {table:<22}{n:>8,}")

    print("\nloading")
    load_databricks.load(force=args.force)
    send_sample_events(db)
    ok = load_databricks.report(db)
    db.close()
    print(f"\nbootstrap finished in {time.perf_counter() - started:.0f}s")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
