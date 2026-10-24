"""Runtime database access (ATLAS-001 FR-28, B-09).

The API and worker connect with the **runtime** role only (``ATLAS_DATABASE_URL``). Alembic uses
the migration URL, never this module. Every probe has a hard wall-clock timeout, and failures are
reported as :class:`DatabaseUnavailable` without the URL, host or driver message, so nothing about
the connection can leak into a response.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from atlas.core.logging import get_logger

log = get_logger("atlas.persistence")

PROBE_TIMEOUT_SECONDS = 1.0
SCHEMA = "atlas"
REVISION_QUERY = text(f"SELECT version_num FROM {SCHEMA}.alembic_version")  # noqa: S608 - constant identifier


class DatabaseUnavailable(Exception):  # noqa: N818 - domain wording ("the database is unavailable")
    """The database did not answer a probe in time, or refused it."""


class DatabaseProbe(Protocol):
    async def ping(self) -> None: ...

    async def migration_revision(self) -> str | None: ...

    async def dispose(self) -> None: ...


class Database:
    """Async SQLAlchemy engine (psycopg 3) on the runtime URL. Connects lazily, on first use."""

    def __init__(self, url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,  # atlas_app is limited to 10 connections on the shared server
            pool_timeout=timeout,
            connect_args={"application_name": "atlas-api"},
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def _scalar(self, statement: object) -> object:
        try:
            async with asyncio.timeout(self._timeout), self._engine.connect() as conn:
                return (await conn.execute(statement)).scalar_one_or_none()  # type: ignore[call-overload]
        except Exception as exc:
            log.warning("database_probe_failed", exc_type=type(exc).__name__)
            raise DatabaseUnavailable from None

    async def ping(self) -> None:
        await self._scalar(text("SELECT 1"))

    async def migration_revision(self) -> str | None:
        """Current Alembic revision read by the runtime role, or None if it cannot be read."""
        try:
            value = await self._scalar(REVISION_QUERY)
        except DatabaseUnavailable:
            return None
        return value if isinstance(value, str) else None

    async def dispose(self) -> None:
        await self._engine.dispose()
