"""Integration fixtures: real PostgreSQL ``atlas_test`` only, as the two test roles (ATLAS-001 CP3).

Settings come from ``ATLAS_ENV_FILE`` (default ``.env.test``) or, in CI, from ``ATLAS_*`` variables.
The settings guards (FR-21) refuse anything but ``ATLAS_ENV=test`` on database ``atlas_test``. A
session guard proves no non-Atlas database or role changed while the tests ran.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from atlas.core.settings import TEST_DATABASE, Environment, Settings, database_name
from atlas.persistence.migrations import SETTINGS_ATTRIBUTE

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
    return loaded


@pytest.fixture(scope="session")
def alembic_config(settings: Settings) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes[SETTINGS_ATTRIBUTE] = settings
    return config


@pytest.fixture(scope="session")
def migrated(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")


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
