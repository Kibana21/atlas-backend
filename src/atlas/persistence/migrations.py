"""Alembic integration (ATLAS-001 FR-28/FR-29, B-10). ``alembic/env.py`` only calls :func:`run`.

* Connects with the **migration** URL only; the migration role owns schema ``atlas`` and every
  object it creates. ``alembic_version`` lives in ``atlas``.
* Constraint naming convention for every future table.
* Rule for every future migration: the runtime role reads by default (schema default privileges);
  any write privilege is granted **per table, explicitly**, in the migration that creates the
  table (reviewed in code review). Canonical tables (ATLAS-002, DEC-044 as amended) are written only
  by :func:`ingest_role`; the runtime role gets no write privilege on them.
* Offline (SQL-script) mode is not supported: migrations always run against a live database.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from sqlalchemy import MetaData, create_engine
from sqlalchemy.pool import NullPool

from atlas.core.settings import Settings, get_settings
from atlas.persistence.canonical import schema_0002
from atlas.persistence.db import SCHEMA

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)
schema_0002.define(metadata)  # what the migrations create; `alembic check` compares the database with it

SETTINGS_ATTRIBUTE = "atlas_settings"
APP_ROLE_ATTRIBUTE = "atlas_db_app_role"
INGEST_ROLE_ATTRIBUTE = "atlas_db_ingest_role"


def include_name(name: str | None, type_: str, _parent_names: Any) -> bool:
    """Compare only schema ``atlas``; the version table is excluded by Alembic itself."""
    return name == SCHEMA if type_ == "schema" else True


def run(context: Any) -> None:
    """Run migrations online. ``context`` is ``alembic.context`` (typed loosely: it is a proxy)."""
    if context.is_offline_mode():
        raise RuntimeError("offline migrations are not supported; run against the database")
    config = context.config
    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    settings: Settings = config.attributes.get(SETTINGS_ATTRIBUTE) or get_settings()
    config.attributes[APP_ROLE_ATTRIBUTE] = settings.db_app_role
    config.attributes[INGEST_ROLE_ATTRIBUTE] = settings.ingestion_role
    # Every migration names schema ``atlas`` explicitly. With the migrator's role default (search_path = atlas) the
    # dialect would take atlas as its default schema, and Alembic would compare it as the unnamed schema; setting
    # search_path for every connection of this engine makes atlas an ordinary named schema for `alembic check`.
    engine = create_engine(
        settings.migration_database_url, poolclass=NullPool, connect_args={"options": "-c search_path=pg_catalog"}
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=metadata,
                version_table_schema=SCHEMA,
                include_schemas=True,
                include_name=include_name,
                compare_type=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


def app_role(context: Any) -> str:
    """Runtime role to name in explicit per-table write grants (``ATLAS_DB_APP_ROLE``)."""
    role = context.config.attributes[APP_ROLE_ATTRIBUTE]
    assert isinstance(role, str)  # noqa: S101 - set by run()
    return role


class IngestRoleMissingError(RuntimeError):
    """Migration 0002 grants canonical writes to the ingestion role; it fails rather than skipping the grants."""


def ingest_role(context: Any) -> str:
    """Ingestion role to name in canonical write grants (``ATLAS_DB_INGEST_ROLE``; DEC-044 as amended)."""
    role = context.config.attributes.get(INGEST_ROLE_ATTRIBUTE)
    if not isinstance(role, str) or not role:
        raise IngestRoleMissingError(
            "ATLAS_DB_INGEST_ROLE is not set: the canonical store needs the ingestion role (owner gate G3-R)"
        )
    return role
