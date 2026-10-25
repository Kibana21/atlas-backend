"""Organization-scoped canonical reads (ADR-0016; ATLAS-002 A002-FR-06).

Every function takes an :class:`OrgScope` as its second argument, and there is no other read path to the canonical
tables: a query without a scope does not type-check. Rows of another organization are never returned, so a lookup
across organizations answers "not found". Reads are bounded by the organization; projections and traversal APIs
are ATLAS-003 (authorization first, invariant 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from atlas.domain.canonical.graph import Dataset, Row
from atlas.persistence.canonical.rows import COLUMNS, from_db

Connection = psycopg.Connection[Any]


def libpq_url(url: str) -> str:
    """psycopg connection string for a settings URL (which uses SQLAlchemy's ``postgresql+psycopg://`` form)."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass(frozen=True)
class OrgScope:
    """The organization every canonical read and write is limited to. Built only by :func:`resolve_scope`."""

    org_id: str
    key: str


class OrganizationNotFound(LookupError):  # noqa: N818 - domain wording
    """No organization with that key (or it is not visible to the caller)."""


def resolve_scope(conn: Connection, key: str) -> OrgScope:
    row = conn.execute("SELECT id, key FROM atlas.organization WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise OrganizationNotFound(key)
    return OrgScope(str(row[0]), row[1])


def _select(conn: Connection, table: str, where: str, params: Sequence[Any]) -> list[Row]:
    sql = f"SELECT {', '.join(COLUMNS[table])} FROM atlas.{table} WHERE org_id = %s AND {where}"  # noqa: S608
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [from_db(table, r) for r in cur.fetchall()]


def current_revision(conn: Connection, scope: OrgScope) -> int:
    row = conn.execute("SELECT last FROM atlas.org_revision WHERE org_id = %s", (scope.org_id,)).fetchone()
    if row is None:
        raise OrganizationNotFound(scope.key)
    return int(row[0])


def entity_version(conn: Connection, scope: OrgScope, entity_id: str, revision: int | None = None) -> Row | None:
    """The version of one entity valid at ``revision`` (default: current), or None if absent in this organization."""
    if revision is None:
        rows = _select(conn, "entity_version", "entity_id = %s AND revision_to IS NULL", (scope.org_id, entity_id))
    else:
        rows = _select(
            conn,
            "entity_version",
            "entity_id = %s AND revision_from <= %s AND (revision_to IS NULL OR revision_to > %s)",
            (scope.org_id, entity_id, revision, revision),
        )
    return rows[0] if rows else None


def load(conn: Connection, scope: OrgScope, *, history: bool = False) -> Dataset:
    """The organization's rows: current versions only (what the publishing procedure needs), or full history.

    Identity rows, change sets and the evidence referenced by the loaded assertions are always included.
    """
    params = (scope.org_id,)
    current = "TRUE" if history else "revision_to IS NULL"
    org = conn.execute("SELECT id, key, display_name FROM atlas.organization WHERE id = %s", params).fetchone()
    if org is None:
        raise OrganizationNotFound(scope.key)
    assertions = _select(conn, "fact_assertion", current, params)
    evidence_ids = sorted({a["evidence_id"] for a in assertions})
    tables = {
        "organizations": [{"id": str(org[0]), "key": org[1], "display_name": org[2]}],
        "change_sets": _select(conn, "change_set", "TRUE", params),
        "entities": _select(conn, "entity", "TRUE", params),
        "entity_versions": _select(conn, "entity_version", current, params),
        "relationships": _select(conn, "relationship", "TRUE", params),
        "relationship_versions": _select(conn, "relationship_version", current, params),
        "external_identities": _select(conn, "external_identity", current, params),
        "evidence": _select(
            conn, "evidence", "TRUE" if history else "id = ANY(%s)", params if history else (scope.org_id, evidence_ids)
        ),
        "fact_assertions": assertions,
        "graph_snapshots": _select(conn, "graph_snapshot", "TRUE", params),
    }
    return Dataset(tables)


def taxonomy_versions(conn: Connection, scope: OrgScope) -> set[str]:
    """Every taxonomy version stamped on the organization's entity and relationship versions, at any revision."""
    rows = conn.execute(
        "SELECT taxonomy_version FROM atlas.entity_version WHERE org_id = %s "
        "UNION SELECT taxonomy_version FROM atlas.relationship_version WHERE org_id = %s",
        (scope.org_id, scope.org_id),
    ).fetchall()
    return {r[0] for r in rows}


def evidence_rows(conn: Connection, scope: OrgScope, ids: list[str]) -> list[Row]:
    """Evidence rows of this organization by ID (IDs of other organizations are not found)."""
    return _select(conn, "evidence", "id = ANY(%s)", (scope.org_id, ids)) if ids else []
