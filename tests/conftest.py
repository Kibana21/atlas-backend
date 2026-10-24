"""Shared test helpers. Unit tests never read .env files or touch external services."""

from __future__ import annotations

from typing import Any

import pytest

from atlas.core.settings import Settings


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


@pytest.fixture(autouse=True)
def _no_ambient_atlas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient ATLAS_* variables must never leak into unit tests."""
    import os

    for key in list(os.environ):
        if key.startswith("ATLAS_"):
            monkeypatch.delenv(key)
