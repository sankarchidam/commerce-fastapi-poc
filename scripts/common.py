"""Helpers shared by the bootstrap / load / simulate scripts."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import Session

ROOT = Path(__file__).resolve().parent.parent
DDL_PATH = ROOT / "sql" / "ddl.sql"
MANAGED_PROPERTY = "commerce_poc.managed"

log = logging.getLogger("scripts")


class UnsafeSchemaError(RuntimeError):
    pass


def render_ddl(settings: Settings, text: str | None = None) -> list[str]:
    """Rewrite the default names in sql/ddl.sql and split into statements."""
    text = text if text is not None else DDL_PATH.read_text()
    cat, core, raw = settings.databricks_catalog, settings.databricks_schema_core, settings.databricks_schema_raw
    metrics = settings.databricks_schema_metrics
    text = (
        text.replace("commerce_poc.core.", f"`{cat}`.`{core}`.")
        .replace("commerce_poc.raw.", f"`{cat}`.`{raw}`.")
        .replace("commerce_poc.metrics.", f"`{cat}`.`{metrics}`.")
        .replace("commerce_poc.core", f"`{cat}`.`{core}`")
        .replace("commerce_poc.raw", f"`{cat}`.`{raw}`")
        .replace("commerce_poc.metrics", f"`{cat}`.`{metrics}`")
        .replace("CATALOG IF NOT EXISTS commerce_poc", f"CATALOG IF NOT EXISTS `{cat}`")
    )
    # Drop full-line comments first so a ';' inside a comment can't split a statement.
    text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


def catalog_exists(s: Session, catalog: str) -> bool:
    rows = s.fetch_all("SHOW CATALOGS")
    return any(next(iter(r.values())) == catalog for r in rows)


def schema_state(s: Session, catalog: str, schema: str) -> tuple[bool, bool, int]:
    """Return (exists, managed_by_this_poc, table_count)."""
    rows = s.fetch_all(f"SHOW SCHEMAS IN `{catalog}`")
    names = {str(next(iter(r.values()))) for r in rows}
    if schema not in names:
        return False, False, 0
    desc = s.fetch_all(f"DESCRIBE SCHEMA EXTENDED `{catalog}`.`{schema}`")
    managed = any(MANAGED_PROPERTY in str(v) for r in desc for v in r.values())
    tables = s.fetch_all(f"SHOW TABLES IN `{catalog}`.`{schema}`")
    return True, managed, len(tables)


def assert_safe_schemas(s: Session, settings: Settings, *, force: bool = False) -> None:
    """Refuse to touch a pre-existing, non-empty schema this POC did not create.

    Matters in shared/sandbox catalogs where `core`, `raw` or `metrics` may already belong to someone.
    """
    for schema in (settings.databricks_schema_core, settings.databricks_schema_raw, settings.databricks_schema_metrics):
        exists, managed, n_tables = schema_state(s, settings.databricks_catalog, schema)
        if exists and not managed and n_tables > 0 and not force:
            raise UnsafeSchemaError(
                f"schema {settings.databricks_catalog}.{schema} already exists with {n_tables} table(s) and was not "
                f"created by this POC. Pick other names via DATABRICKS_SCHEMA_CORE / _RAW / _METRICS, "
                f"or pass --force if you really mean to use it."
            )
        if exists and not managed:
            s.execute(
                f"ALTER SCHEMA `{settings.databricks_catalog}`.`{schema}` "
                f"SET DBPROPERTIES ('{MANAGED_PROPERTY}' = 'true')"
            )


def sql_literal(value: Any, sql_type: str) -> str:
    """Render a trusted, generated value as a Databricks SQL literal (bulk loading only).

    The query API never uses this: it binds parameters. It exists because inserting
    tens of thousands of seed rows one parameterized statement at a time is far too slow.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "NULL"
    try:
        import pandas as pd

        if value is pd.NaT or (not isinstance(value, (str, bytes, Decimal)) and pd.isna(value)):
            return "NULL"
    except (TypeError, ValueError):
        pass
    t = sql_type.upper()
    if t == "BOOLEAN":
        return "TRUE" if bool(value) else "FALSE"
    if t in ("INT", "BIGINT"):
        return str(int(value))
    if t.startswith("DECIMAL"):
        return f"CAST({Decimal(str(value))} AS {t})"
    if t == "TIMESTAMP":
        if isinstance(value, (datetime, date)):
            text = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
        else:
            text = str(value)
        return f"TIMESTAMP '{_escape(text)}'"
    return f"'{_escape(str(value))}'"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")
