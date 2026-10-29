"""Application settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    databricks_host: str = ""
    databricks_http_path: str = ""
    databricks_token: str = Field(default="", repr=False)
    databricks_catalog: str = "commerce_poc"
    databricks_schema_core: str = "core"
    databricks_schema_raw: str = "raw"

    shopify_webhook_secret: str = Field(default="dev-webhook-secret", repr=False)
    api_key: str = Field(default="dev-api-key", repr=False)

    # sync: apply to core inside the webhook request (spec default; ~6-10 s on serverless).
    # background: 200 right after the raw event is stored, apply after the response is sent.
    webhook_apply_mode: Literal["sync", "background"] = "sync"
    default_currency: str = "USD"
    db_pool_size: int = 8
    log_level: str = "INFO"

    @field_validator("databricks_catalog", "databricks_schema_core", "databricks_schema_raw")
    @classmethod
    def _identifier(cls, value: str) -> str:
        # These are interpolated into SQL as identifiers, so they must be plain names.
        if not _IDENTIFIER.match(value):
            raise ValueError(f"invalid SQL identifier: {value!r}")
        return value

    @field_validator("databricks_host")
    @classmethod
    def _strip_scheme(cls, value: str) -> str:
        return value.removeprefix("https://").removeprefix("http://").rstrip("/")

    @property
    def core(self) -> str:
        return f"`{self.databricks_catalog}`.`{self.databricks_schema_core}`"

    @property
    def raw(self) -> str:
        return f"`{self.databricks_catalog}`.`{self.databricks_schema_raw}`"

    @property
    def databricks_configured(self) -> bool:
        return bool(self.databricks_host and self.databricks_http_path and self.databricks_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
