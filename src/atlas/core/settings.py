"""Validated runtime settings with environment guards (ATLAS-001 FR-21, spec §6.3).

All configuration comes from ``ATLAS_*`` environment variables (optionally a dotenv file named by
``ATLAS_ENV_FILE``). Guards make unsafe combinations impossible to start with, e.g. tests that
would touch the local database, or diagnostics enabled in uat/prod.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    UAT = "uat"
    PROD = "prod"


TEST_DATABASE = "atlas_test"
LOCAL_REDIS_DB = 10
TEST_REDIS_DB = 11
TEST_PREFIX = "atlas:test:"
LOCAL_PREFIX = "atlas:local:"
RESTRICTED_ENVIRONMENTS = frozenset({Environment.UAT, Environment.PROD})


class SettingsError(ValueError):
    """Raised when configuration is unsafe or inconsistent."""


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def redis_db(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path else 0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_", extra="ignore", frozen=True)

    env: Environment
    database_url: str = Field(description="Runtime role connection URL (API and worker)")
    migration_database_url: str = Field(description="Migration role connection URL (Alembic only)")
    db_app_role: str = Field(description="Runtime role name; grantee of per-table write grants in migrations")
    redis_url: str
    redis_key_prefix: str
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ()
    diagnostics_enabled: bool = False
    build_sha: str | None = None
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _postgres_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+psycopg://")) or not database_name(value):
            raise ValueError("must be a postgresql:// URL that names a database")
        return value

    @field_validator("redis_url")
    @classmethod
    def _redis_url(cls, value: str) -> str:
        if not value.startswith("redis://"):
            raise ValueError("must be a redis:// URL")
        redis_db(value)  # raises ValueError for a non-numeric DB index
        return value

    @model_validator(mode="after")
    def _environment_guards(self) -> Self:
        problems: list[str] = []
        dbs = {database_name(self.database_url), database_name(self.migration_database_url)}
        rdb = redis_db(self.redis_url)
        if self.env is Environment.TEST:
            if dbs != {TEST_DATABASE}:
                problems.append(f"test runs must use database {TEST_DATABASE!r} for both roles, got {sorted(dbs)}")
            if rdb != TEST_REDIS_DB:
                problems.append(f"test runs must use Redis DB {TEST_REDIS_DB}, got {rdb}")
            if not self.redis_key_prefix.startswith(TEST_PREFIX):
                problems.append(f"test Redis keys must start with {TEST_PREFIX!r}")
        else:
            if TEST_DATABASE in dbs:
                problems.append(f"{self.env.value} must not use the test database {TEST_DATABASE!r}")
            if rdb == TEST_REDIS_DB:
                problems.append(f"{self.env.value} must not use the test Redis DB {TEST_REDIS_DB}")
        if self.env is Environment.LOCAL:
            if rdb != LOCAL_REDIS_DB:
                problems.append(f"local runs must use Redis DB {LOCAL_REDIS_DB}, got {rdb}")
            if not self.redis_key_prefix.startswith(LOCAL_PREFIX):
                problems.append(f"local Redis keys must start with {LOCAL_PREFIX!r}")
        if not self.redis_key_prefix.startswith("atlas:") or not self.redis_key_prefix.endswith(":"):
            problems.append("redis_key_prefix must start with 'atlas:' and end with ':'")
        if self.diagnostics_enabled and self.env in RESTRICTED_ENVIRONMENTS:
            problems.append(f"diagnostics must not be enabled in {self.env.value}")
        if problems:
            raise SettingsError("; ".join(problems))
        return self

    @property
    def docs_enabled(self) -> bool:
        return self.env is Environment.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.environ.get("ATLAS_ENV_FILE", ".env")
    return Settings(_env_file=env_file if os.path.exists(env_file) else None)
