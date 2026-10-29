"""Bulk-load data/generated/*.parquet into the core schema.

Replaces the contents of every core table (TRUNCATE + multi-row INSERT in chunks), then
prints row counts and a few sanity queries.

    python -m scripts.load_databricks
    python -m scripts.load_databricks --chunk-size 500
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.db import Database, Session
from app.tables import CORE_LOAD_ORDER, CORE_TABLES
from scripts.common import ROOT, assert_safe_schemas, sql_literal

DEFAULT_DIR = ROOT / "data" / "generated"

SANITY_QUERIES = {
    "orders whose total != sum(lines) + shipping": """
        SELECT COUNT(*) AS n FROM {core}.orders o
        JOIN (SELECT order_id, SUM(line_total) AS s FROM {core}.order_details GROUP BY order_id) d
          ON d.order_id = o.order_id
        WHERE ABS(o.total - (d.s + o.shipping)) > 0.01""",
    "order_details with unknown order/variant": """
        SELECT COUNT(*) AS n FROM {core}.order_details d
        LEFT JOIN {core}.orders o ON o.order_id = d.order_id
        LEFT JOIN {core}.item_variants v ON v.variant_id = d.variant_id
        WHERE o.order_id IS NULL OR v.variant_id IS NULL""",
    "orders with unknown customer": """
        SELECT COUNT(*) AS n FROM {core}.orders o
        LEFT JOIN {core}.customers c ON c.customer_id = o.customer_id
        WHERE c.customer_id IS NULL""",
    "refund_details with unknown order_detail": """
        SELECT COUNT(*) AS n FROM {core}.refund_details r
        LEFT JOIN {core}.order_details d ON d.order_detail_id = r.order_detail_id
        WHERE d.order_detail_id IS NULL""",
    "orders in a currency missing from fx_rates (dropped from *_usd metrics)": """
        SELECT COUNT(*) AS n FROM {core}.orders o
        LEFT JOIN {core}.fx_rates f ON f.currency = o.currency
        WHERE f.currency IS NULL""",
    # Seeded variants only: webhook-created variants have no movement ledger.
    "seeded variants where ledger != inventory_quantity": """
        SELECT COUNT(*) AS n FROM {core}.item_variants v
        JOIN (SELECT variant_id, SUM(quantity_delta) AS q FROM {core}.inventory_movements GROUP BY variant_id) m
          ON m.variant_id = v.variant_id
        WHERE m.q <> v.inventory_quantity""",
}


def insert_frame(s: Session, fq_table: str, table: str, df: pd.DataFrame, chunk_size: int) -> int:
    spec = CORE_TABLES[table]
    cols = ", ".join(f"`{c}`" for c, _ in spec)
    loaded = 0
    for start in range(0, len(df), chunk_size):
        chunk = df.iloc[start : start + chunk_size]
        values = ",\n".join(
            "(" + ", ".join(sql_literal(row[c], t) for c, t in spec) + ")"
            for row in chunk.to_dict("records")
        )
        s.execute(f"INSERT INTO {fq_table} ({cols}) VALUES\n{values}")
        loaded += len(chunk)
    return loaded


def load(data_dir: Path = DEFAULT_DIR, chunk_size: int = 1000, force: bool = False) -> dict[str, int]:
    settings = get_settings()
    db = Database(settings)
    counts: dict[str, int] = {}
    with db.session() as s:
        assert_safe_schemas(s, settings, force=force)
        for table in reversed(CORE_LOAD_ORDER):
            s.execute(f"TRUNCATE TABLE {settings.core}.`{table}`")
        for table in CORE_LOAD_ORDER:
            path = data_dir / f"{table}.parquet"
            if not path.exists():
                sys.exit(f"missing {path}; run `python -m scripts.generate_seed_data` first")
            df = pd.read_parquet(path)
            t0 = time.perf_counter()
            n = insert_frame(s, f"{settings.core}.`{table}`", table, df, chunk_size)
            counts[table] = n
            print(f"loaded {table:<22}{n:>8,} rows in {time.perf_counter() - t0:5.1f}s")
    db.close()
    return counts


def report(db: Database) -> bool:
    ok = True
    with db.session() as s:
        print("\nrow counts")
        for table in CORE_LOAD_ORDER:
            n = s.fetch_one(f"SELECT COUNT(*) AS n FROM {db.settings.core}.`{table}`")["n"]
            print(f"  {table:<22}{n:>8,}")
        print("\nsanity checks (all should be 0)")
        for label, sql in SANITY_QUERIES.items():
            n = s.fetch_one(sql.replace("{core}", db.settings.core))["n"]
            ok &= n == 0
            print(f"  {'OK ' if n == 0 else 'BAD'} {label}: {n}")
        overview = s.fetch_one(
            f"""SELECT COUNT(*) AS orders,
                       CAST(SUM(CASE WHEN status <> 'cancelled' THEN total END) AS DECIMAL(18,2)) AS gmv,
                       MIN(ordered_at) AS first_order, MAX(ordered_at) AS last_order
                FROM {db.settings.core}.orders"""
        )
        print(f"\n  orders={overview['orders']:,} gmv={overview['gmv']} "
              f"range={overview['first_order']} .. {overview['last_order']}")
        status_rows = s.fetch_all(
            f"SELECT status, COUNT(*) AS n FROM {db.settings.core}.orders GROUP BY status ORDER BY n DESC"
        )
        print("  orders by status: " + ", ".join(f"{r['status']}={r['n']}" for r in status_rows))
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--force", action="store_true", help="allow loading into a schema this POC did not create")
    args = parser.parse_args()
    load(args.data_dir, args.chunk_size, args.force)
    db = Database(get_settings())
    ok = report(db)
    db.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
