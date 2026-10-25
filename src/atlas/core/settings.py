"""Validated runtime settings with environment guards (ATLAS-001 FR-21, spec §6.3).

All configuration comes from ``ATLAS_*`` environment variables (optionally a dotenv file named by
``ATLAS_ENV_FILE``). Guards make unsafe combinations impossible to start with, e.g. tests that
would touch the local database, or diagnostics enabled in uat/prod.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self
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
# Owner decision (25 Oct 2026, ATLAS-002): local development and its integration tests may use one database account
# for every role. Never in dev/uat/prod: that keeps the six-role separation (DEC-044) as the hardening option.
SINGLE_USER_ENVIRONMENTS = frozenset({Environment.LOCAL, Environment.TEST})


def database_user(url: str) -> str | None:
    return urlsplit(url).username


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
    # Ingestion identity (ATLAS-002, DEC-044 as amended). The API never reads the URL: only migrations (the role
    # name, for canonical grants) and ingestion entry points (CP4) use them. Absent until the owner's gate G3-R.
    ingest_database_url: str | None = Field(default=None, description="Ingestion role connection URL")
    db_ingest_role: str | None = Field(default=None, description="Ingestion role name; grantee of canonical writes")
    # "separated": runtime, migration and ingestion are different roles (DEC-044). "single": one account for all of
    # them, local and test only (owner decision, 25 Oct 2026); database privileges then separate nothing.
    db_role_model: Literal["separated", "single"] = "separated"
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

    @field_validator("database_url", "migration_database_url", "ingest_database_url")
    @classmethod
    def _postgres_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
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
        if self.ingest_database_url is not None:
            dbs.add(database_name(self.ingest_database_url))
        rdb = redis_db(self.redis_url)
        if self.env is Environment.TEST:
            if dbs != {TEST_DATABASE}:
                problems.append(f"test runs must use database {TEST_DATABASE!r} for every role, got {sorted(dbs)}")
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
        if self.db_role_model == "single":
            problems += self._single_user_problems()
        elif self.ingest_database_url is not None:
            ingest_user = database_user(self.ingest_database_url)
            if self.db_ingest_role is None or ingest_user != self.db_ingest_role:
                problems.append("ingest_database_url must connect as db_ingest_role")
            if ingest_user in {database_user(self.database_url), database_user(self.migration_database_url)}:
                problems.append("the ingestion role must differ from the runtime and migration roles")
        if self.diagnostics_enabled and self.env in RESTRICTED_ENVIRONMENTS:
            problems.append(f"diagnostics must not be enabled in {self.env.value}")
        if problems:
            raise SettingsError("; ".join(problems))
        return self

    def _single_user_problems(self) -> list[str]:
        problems = []
        if self.env not in SINGLE_USER_ENVIRONMENTS:
            problems.append(f"db_role_model 'single' is for local development and tests only, not {self.env.value}")
        urls = [self.database_url, self.migration_database_url, self.ingest_database_url]
        users = {database_user(u) for u in urls if u is not None}
        if len(users) != 1 or None in users:
            problems.append("db_role_model 'single' needs one database user in every URL")
        user = next(iter(users))
        if self.db_app_role != user or self.db_ingest_role not in (None, user):
            problems.append("db_role_model 'single' needs db_app_role (and db_ingest_role, if set) equal to that user")
        return problems

    @property
    def ingestion_url(self) -> str | None:
        """Connection for ingestion entry points: the ingestion role, or in the single-user model the one account."""
        if self.ingest_database_url is not None or self.db_role_model == "separated":
            return self.ingest_database_url
        return self.migration_database_url

    @property
    def ingestion_role(self) -> str | None:
        """Grantee of canonical writes in migrations (separated model) or the one account (single-user model)."""
        if self.db_ingest_role is not None or self.db_role_model == "separated":
            return self.db_ingest_role
        return database_user(self.migration_database_url)

    @property
    def docs_enabled(self) -> bool:
        return self.env is Environment.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.environ.get("ATLAS_ENV_FILE", ".env")
    return Settings(_env_file=env_file if os.path.exists(env_file) else None)
