"""Unit-test isolation: no ambient ATLAS_* settings, no real database engine, no real Redis client."""

from __future__ import annotations

import os
from typing import Any

import pytest
import redis.asyncio

import atlas.main
import atlas.persistence.db
from tests.conftest import FakeDatabase, FakeRedisProbe


@pytest.fixture(autouse=True)
def _no_ambient_atlas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient ATLAS_* variables must never leak into unit tests."""
    for key in list(os.environ):
        if key.startswith("ATLAS_"):
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app() gets an unavailable fake unless a test injects one; engines cannot be created."""

    def refuse_engine(*_: Any, **__: Any) -> Any:
        raise AssertionError("unit tests must not create a database engine")

    monkeypatch.setattr(atlas.main, "Database", lambda _url: FakeDatabase(available=False))
    monkeypatch.setattr(atlas.persistence.db, "create_async_engine", refuse_engine)


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app() gets an unavailable Redis fake unless injected; real clients cannot be created."""

    def refuse_client(*_: Any, **__: Any) -> Any:
        raise AssertionError("unit tests must not create a Redis client")

    monkeypatch.setattr(atlas.main, "create_redis", lambda _settings: FakeRedisProbe(available=False))
    monkeypatch.setattr(redis.asyncio.Redis, "from_url", refuse_client)
