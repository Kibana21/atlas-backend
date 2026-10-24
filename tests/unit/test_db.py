"""atlas.persistence.db probes: timeouts and failures never leak connection details (FR-26, FR-28)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

import atlas.persistence.db as db

pytestmark = pytest.mark.unit
URL = "postgresql+psycopg://atlas_test_app:secret@localhost:5432/atlas_test"


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Conn:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour

    async def execute(self, statement: object) -> _Result:
        return _Result(await self.behaviour(str(statement)))


class _Engine:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.disposed = False

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_Conn]:
        yield _Conn(self.behaviour)

    async def dispose(self) -> None:
        self.disposed = True


def _database(monkeypatch: pytest.MonkeyPatch, behaviour: Any, timeout: float = 1.0) -> tuple[db.Database, _Engine]:
    engine = _Engine(behaviour)
    seen: dict[str, Any] = {}

    def fake_create(url: str, **kwargs: Any) -> _Engine:
        seen.update(url=url, **kwargs)
        return engine

    monkeypatch.setattr(db, "create_async_engine", fake_create)
    database = db.Database(URL, timeout=timeout)
    assert seen["url"] == URL
    assert seen["pool_pre_ping"] is True
    assert seen["pool_size"] + seen["max_overflow"] <= 10  # atlas_app connection limit
    return database, engine


@pytest.mark.anyio
async def test_ping_and_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def answer(sql: str) -> object:
        return "0001_baseline" if "alembic_version" in sql else 1

    database, engine = _database(monkeypatch, answer)
    await database.ping()
    assert await database.migration_revision() == "0001_baseline"
    assert database.engine is engine  # type: ignore[comparison-overlap]
    await database.dispose()
    assert engine.disposed


@pytest.mark.anyio
async def test_failure_is_reported_without_driver_details(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(_: str) -> object:
        raise OSError("connection to server at localhost:5432 as atlas_test_app failed")

    database, _ = _database(monkeypatch, fail)
    with pytest.raises(db.DatabaseUnavailable) as caught:
        await database.ping()
    assert str(caught.value) == ""
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert await database.migration_revision() is None


@pytest.mark.anyio
async def test_slow_database_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(_: str) -> object:
        await asyncio.sleep(5)
        return 1

    database, _ = _database(monkeypatch, hang, timeout=0.05)
    with pytest.raises(db.DatabaseUnavailable):
        await database.ping()


@pytest.mark.anyio
async def test_non_text_revision_is_treated_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(_: str) -> object:
        return None

    database, _ = _database(monkeypatch, none)
    assert await database.migration_revision() is None
