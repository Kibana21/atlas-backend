"""Shared test helpers. Unit tests never read .env files or touch external services (tests/unit/conftest.py)."""

from __future__ import annotations

from typing import Any

import pytest

from atlas.core.settings import Settings
from atlas.persistence.db import DatabaseUnavailable


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "database_url": "postgresql+psycopg://atlas_test_app:x@localhost:5432/atlas_test",
        "migration_database_url": "postgresql+psycopg://atlas_test_migrator:x@localhost:5432/atlas_test",
        "db_app_role": "atlas_test_app",
        "redis_url": "redis://localhost:6379/11",
        "redis_key_prefix": "atlas:test:unit:",
        "cors_origins": ("http://localhost:3000",),
        "diagnostics_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeDatabase:
    """In-memory stand-in for atlas.persistence.db.Database (unit tests only)."""

    def __init__(self, *, available: bool = True, revision: str | None = None) -> None:
        self.available = available
        self.revision = revision
        self.disposed = False

    async def ping(self) -> None:
        if not self.available:
            raise DatabaseUnavailable

    async def migration_revision(self) -> str | None:
        return self.revision if self.available else None

    async def dispose(self) -> None:
        self.disposed = True
