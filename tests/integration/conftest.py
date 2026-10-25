"""Integration fixtures: PostgreSQL ``atlas_test`` (two test roles) and Redis DB 11 (ATLAS-001 CP3/CP4).

Settings come from ``ATLAS_ENV_FILE`` (default ``.env.test``) or, in CI, from ``ATLAS_*`` variables.
The settings guards (FR-21) refuse anything but ``ATLAS_ENV=test`` on database ``atlas_test`` and
Redis DB 11. Every session gets its own key prefix ``atlas:test:<run_id>:``.

Session guards prove that nothing outside Atlas changed:

* PostgreSQL: non-Atlas databases, ACLs, roles and role settings are identical before and after;
* Redis: no key is left under the run prefix, and the key **counts** (``DBSIZE``) of DB 1, DB 10 and
  DB 11 are unchanged. Keys outside the Atlas prefix are never listed, read or deleted.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
import redis
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from atlas.core.ids import uuid7
from atlas.core.settings import TEST_DATABASE, TEST_REDIS_DB, Environment, Settings, database_name, redis_db
from atlas.persistence.migrations import SETTINGS_ATTRIBUTE
from atlas.persistence.redis import create_redis

ROOT = Path(__file__).resolve().parents[2]
ATLAS_DATABASES = frozenset({"atlas_db", TEST_DATABASE})


def connect_kwargs(url: str, *, dbname: str | None = None, password: str | None = None) -> dict[str, Any]:
    """psycopg keyword arguments for a settings URL (optionally another Atlas database/password)."""
    u = make_url(url)
    target = dbname or u.database
    assert target in ATLAS_DATABASES, f"integration tests connect to Atlas databases only, not {target!r}"
    return {
        "host": u.host,
        "port": u.port or 5432,
        "user": u.username,
        "password": u.password if password is None else password,
        "dbname": target,
        "connect_timeout": 5,
    }


def connect(url: str, **overrides: Any) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(**connect_kwargs(url, **overrides), autocommit=True)


@pytest.fixture(scope="session")
def settings() -> Settings:
    env_file = os.environ.get("ATLAS_ENV_FILE", str(ROOT / ".env.test"))
    loaded = Settings(_env_file=env_file if os.path.exists(env_file) else None)
    assert loaded.env is Environment.TEST
    assert database_name(loaded.database_url) == database_name(loaded.migration_database_url) == TEST_DATABASE
    assert redis_db(loaded.redis_url) == TEST_REDIS_DB
    run_prefix = f"atlas:test:{uuid7().hex[:16]}:"  # FR-33: per-session prefix
    return loaded.model_copy(update={"redis_key_prefix": run_prefix})


@pytest.fixture(scope="session")
def alembic_config(settings: Settings) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes[SETTINGS_ATTRIBUTE] = settings
    return config


@pytest.fixture(scope="session")
def migrated(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")


@pytest.fixture(scope="session")
def ingest_url(settings: Settings) -> str:
    """The ingestion connection (ATLAS-002): the ingestion role, or in the single-user model the one account."""
    url = settings.ingestion_url
    if url is None or settings.ingestion_role is None:
        pytest.fail("ATLAS_INGEST_DATABASE_URL / ATLAS_DB_INGEST_ROLE not set (separated model) and not single-user")
    return url


SINGLE_USER_SKIP = (
    "single-user model (owner decision, 25 Oct 2026): database role separation is not configured locally; "
    "CI's separated-roles run verifies it"
)


@pytest.fixture(autouse=True)
def _role_separation_only(request: pytest.FixtureRequest, settings: Settings) -> None:
    if request.node.get_closest_marker("role_separation") and settings.db_role_model == "single":
        pytest.skip(SINGLE_USER_SKIP)


def tx_connect(url: str) -> psycopg.Connection[tuple[Any, ...]]:
    """A non-autocommit connection: tests wrap their writes in ``transaction(force_rollback=True)``."""
    return psycopg.connect(**connect_kwargs(url))


def _inventory(settings: Settings) -> tuple[Any, ...]:
    with connect(settings.database_url) as conn:
        databases = conn.execute(
            "SELECT datname, datdba::regrole::text, datacl::text FROM pg_database WHERE datname <> ALL(%s) ORDER BY 1",
            (sorted(ATLAS_DATABASES),),
        ).fetchall()
        roles = conn.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles "
            "WHERE rolname !~ '^atlas_' ORDER BY 1"
        ).fetchall()
        settings_rows = conn.execute(
            "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
            "WHERE r.rolname !~ '^atlas_'"
        ).fetchall()
    return tuple(databases), tuple(roles), tuple(settings_rows)


@pytest.fixture(scope="session", autouse=True)
def _non_atlas_objects_unchanged(settings: Settings) -> Iterator[None]:
    before = _inventory(settings)
    yield
    assert _inventory(settings) == before, "a non-Atlas database, role or role setting changed during the tests"


GUARDED_REDIS_DBS = (1, 10, TEST_REDIS_DB)


def redis_dbsizes(settings: Settings) -> dict[int, int]:
    """Key counts of the guarded DBs (DBSIZE only: no key of another application is listed or read)."""
    sizes = {}
    for db in GUARDED_REDIS_DBS:
        url = urlsplit(settings.redis_url)  # explicit db: from_url would let the URL path win over db=
        client = redis.Redis(host=url.hostname or "localhost", port=url.port or 6379, db=db, socket_timeout=2)
        try:
            sizes[db] = int(client.dbsize())
        finally:
            client.close()
    return sizes


def run_keys(settings: Settings) -> list[str]:
    async def scan() -> list[str]:
        client = create_redis(settings, timeout=2.0)
        try:
            return await client.scan_prefix()
        finally:
            await client.close()

    return asyncio.run(scan())


@pytest.fixture(scope="session", autouse=True)
def _redis_outside_the_run_unchanged(settings: Settings) -> Iterator[None]:
    before = redis_dbsizes(settings)
    yield
    assert run_keys(settings) == [], "keys left under the run prefix"
    assert redis_dbsizes(settings) == before, "a Redis key count changed outside this run's own keys"


@pytest.fixture
def key_tracker(settings: Settings) -> Iterator[list[str]]:
    """Tests append every key they cause to exist; exactly those keys are deleted afterwards."""
    keys: list[str] = []
    yield keys

    async def delete() -> None:
        client = create_redis(settings, timeout=2.0)
        try:
            await client.delete_exact(*keys)
        finally:
            await client.close()

    if keys:
        asyncio.run(delete())
