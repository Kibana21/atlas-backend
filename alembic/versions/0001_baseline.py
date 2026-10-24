"""Baseline: empty schema ``atlas`` (ATLAS-001 FR-29).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-24

Reviewed empty baseline. It creates no tables and grants nothing: the only object afterwards is
``atlas.alembic_version``, owned by the migration role and readable by the runtime role through
the schema's default privileges. Domain and job tables arrive in later work items.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
