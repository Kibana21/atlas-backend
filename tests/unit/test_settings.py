"""Settings guards (FR-21, AC-07, AC-11, AC-09)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.core.settings import Environment, get_settings
from tests.conftest import make_settings

LOCAL = {
    "env": "local",
    "database_url": "postgresql+psycopg://atlas_app:x@localhost:5432/atlas_db",
    "migration_database_url": "postgresql+psycopg://atlas_migrator:x@localhost:5432/atlas_db",
    "db_app_role": "atlas_app",
    "redis_url": "redis://localhost:6379/10",
    "redis_key_prefix": "atlas:local:",
}

pytestmark = pytest.mark.unit


def test_valid_test_settings() -> None:
    s = make_settings()
    assert s.env is Environment.TEST
    assert s.cors_origins == ("http://localhost:3000",)
    assert not s.docs_enabled


def test_valid_local_settings_enable_docs() -> None:
    s = make_settings(**LOCAL)
    assert s.docs_enabled


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"database_url": "postgresql+psycopg://atlas_test_app:x@localhost:5432/atlas_db"},
            "test runs must use database",
        ),
        ({"redis_url": "redis://localhost:6379/10"}, "test runs must use Redis DB 11"),
        ({"redis_key_prefix": "atlas:local:"}, "test Redis keys must start with"),
        (
            {**LOCAL, "database_url": "postgresql+psycopg://atlas_app:x@localhost:5432/atlas_test"},
            "must not use the test database",
        ),
        ({**LOCAL, "redis_url": "redis://localhost:6379/11"}, "must not use the test Redis DB 11"),
        ({**LOCAL, "redis_url": "redis://localhost:6379/1"}, "local runs must use Redis DB 10"),
        ({**LOCAL, "redis_key_prefix": "app:"}, "must start with 'atlas:'"),
        (
            {
                **LOCAL,
                "env": "prod",
                "diagnostics_enabled": True,
                "redis_url": "redis://r:6379/0",
                "redis_key_prefix": "atlas:prod:",
            },
            "diagnostics must not be enabled in prod",
        ),
        (
            {
                **LOCAL,
                "env": "uat",
                "diagnostics_enabled": True,
                "redis_url": "redis://r:6379/0",
                "redis_key_prefix": "atlas:uat:",
            },
            "diagnostics must not be enabled in uat",
        ),
    ],
)
def test_unsafe_combinations_are_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**overrides)


def test_prod_without_diagnostics_is_allowed() -> None:
    s = make_settings(
        **{
            **LOCAL,
            "env": "prod",
            "diagnostics_enabled": False,
            "redis_url": "redis://r:6379/0",
            "redis_key_prefix": "atlas:prod:",
        }
    )
    assert s.env is Environment.PROD


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "mysql://x/y"),
        ("database_url", "postgresql://localhost:5432/"),
        ("redis_url", "http://r/1"),
        ("redis_url", "redis://r:6379/abc"),
    ],
)
def test_malformed_urls_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_cors_origins_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {**LOCAL, "cors_origins": "http://a.test, http://b.test"}.items():
        monkeypatch.setenv(f"ATLAS_{key.upper()}", value)
    monkeypatch.setenv("ATLAS_ENV_FILE", "/nonexistent/.env")
    get_settings.cache_clear()
    try:
        assert get_settings().cors_origins == ("http://a.test", "http://b.test")
    finally:
        get_settings.cache_clear()
