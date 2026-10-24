"""Alembic environment: all logic lives in atlas.persistence.migrations (typed, reviewed)."""

from alembic import context

from atlas.persistence.migrations import run

run(context)
