"""AC-CP3-05 / A2-T15: the golden fixture's rows load into atlas_test, and the HARD rules hold as SQL.

The rows are written in one transaction as the migration role (organizations are created by migrations only, so
the ingestion role cannot create the fixture's two organizations) and rolled back at the end. The fixture's
organization keys are prefixed with ``golden-`` because migration 0002 already created the ``default``
organization; IDs and every other value are unchanged. The test overlay's synthetic taxonomy version is registered
in the same transaction.

Checks: the database accepts every row (keys, composite FKs, CHECKs, partial unique indexes, the triggers), the
integrity SQL finds nothing at any revision, and the rows read back through the organization-scoped repository are
equal to the fixture (``created_at``/``db_role`` aside, which the server sets) and reproduce every as-of graph hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
from atlas.domain.canonical.graph import TABLES, as_of, graph_hash
from atlas.persistence.canonical import integrity, repository
from atlas.persistence.canonical.repository import OrgScope
from atlas.persistence.canonical.rows import FIXTURE_TABLE, insert_sql, to_db
from tests.integration.conftest import tx_connect

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "build" / "specs" / "acceptance" / "test-data" / "canonical-golden-v1"
LOAD_ORDER = (
    "change_sets",
    "evidence",
    "entities",
    "entity_versions",
    "relationships",
    "relationship_versions",
    "external_identities",
    "fact_assertions",
    "graph_snapshots",
)


def fixture() -> dict[str, list[dict[str, Any]]]:
    if not FIXTURE.exists():
        pytest.fail("pinned golden fixture missing: run `make specs`")
    return {t: json.loads((FIXTURE / f"{t}.json").read_text(encoding="utf-8")) for t in TABLES}


def load(conn: psycopg.Connection[Any], tables: dict[str, list[dict[str, Any]]]) -> dict[str, OrgScope]:
    for path in sorted(FIXTURE.glob("taxonomy-overlay.*.json")):
        overlay = json.loads(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO atlas.taxonomy_release (version, status) VALUES (%s, %s)",
            (overlay["taxonomy_version"], overlay["status"]),
        )
        for provider, items in overlay.get("add_native_type_mappings", {}).items():
            for m in items:
                conn.execute(
                    "INSERT INTO atlas.native_type_mapping (taxonomy_version, provider, native_type, category) "
                    "VALUES (%s, %s, %s, %s)",
                    (overlay["taxonomy_version"], provider, m["native_type"], m["category"]),
                )
    scopes = {}
    for org in tables["organizations"]:
        key = f"golden-{org['key']}"
        head = max(c["revision"] for c in tables["change_sets"] if c["org_id"] == org["id"])
        conn.execute(
            "INSERT INTO atlas.organization (id, key, display_name) VALUES (%s, %s, %s)",
            (org["id"], key, org["display_name"]),
        )
        conn.execute("INSERT INTO atlas.org_revision (org_id, last) VALUES (%s, %s)", (org["id"], head))
        scopes[org["key"]] = OrgScope(org["id"], key)
    rel_types = {r["id"]: r["rel_type"] for r in tables["relationships"]}
    for name in LOAD_ORDER:
        rows = sorted(tables[name], key=lambda r: (r["org_id"], r.get("revision", 0)))
        table = FIXTURE_TABLE[name]
        with conn.cursor() as cur:
            cur.executemany(insert_sql(table), [to_db(table, r, rel_types=rel_types) for r in rows])
    return scopes


def key(r: dict[str, Any]) -> tuple[Any, ...]:
    return (r.get("id") or r.get("entity_id") or r.get("relationship_id"), r.get("revision_from"), r.get("revision"))


def _without_server_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k not in ("created_at", "db_role")} for r in rows]


def test_golden_rows_load_and_pass_the_sql_integrity_checks(migrated: None, settings: Settings) -> None:
    tables = fixture()
    expected = json.loads((FIXTURE / "expected-as-of.json").read_text(encoding="utf-8"))
    with tx_connect(settings.migration_database_url) as conn, conn.transaction(force_rollback=True):
        scopes = load(conn, tables)
        for org_key, scope in scopes.items():
            head = repository.current_revision(conn, scope)
            problems = [p for rev in range(1, head + 1) for p in integrity.revision_findings(conn, scope, rev)]
            problems += integrity.history_findings(conn, scope)
            assert problems == [], problems
            loaded = repository.load(conn, scope, history=True)
            for name in TABLES:
                if name == "organizations":
                    continue
                mine = [r for r in tables[name] if r["org_id"] == scope.org_id]
                db_rows, fx_rows = sorted(loaded.tables[name], key=key), sorted(mine, key=key)
                if name == "change_sets":
                    assert {r["db_role"] for r in db_rows} == {make_url(settings.migration_database_url).username}
                    db_rows, fx_rows = _without_server_fields(db_rows), _without_server_fields(fx_rows)
                assert db_rows == fx_rows, name
            for e in (x for x in expected if x["org_key"] == org_key):
                st = as_of(loaded, scope.org_id, e["revision"])
                assert (len(st.entities), len(st.relationships), graph_hash(st)) == (
                    e["entities"],
                    e["relationships"],
                    e["graph_hash"],
                ), e


def test_integrity_sql_detects_a_dangling_relationship(migrated: None, settings: Settings) -> None:
    tables = fixture()
    with tx_connect(settings.migration_database_url) as conn, conn.transaction(force_rollback=True):
        scopes = load(conn, tables)
        scope = scopes["default"]
        head = repository.current_revision(conn, scope)
        target = next(
            r for r in tables["relationships"] if r["org_id"] == scope.org_id and r["rel_type"] == "WRITES_TO"
        )
        conn.execute(  # as the owner, inside the rolled-back test transaction only
            "INSERT INTO atlas.change_set (org_id, revision, kind, channel, actor_kind, actor_namespace, actor_key, "
            "source_ref, reason) VALUES (%s, %s, 'steward_edit', 'api', 'human', 'oidc', "
            "'https://login.example.invalid/synthetic|steward-0001', 'test', 'test')",
            (scope.org_id, head + 1),
        )
        conn.execute(
            "UPDATE atlas.entity_version SET revision_to = %s WHERE entity_id = %s AND revision_to IS NULL",
            (head + 1, target["to_id"]),
        )
        problems = integrity.revision_findings(conn, scope, head + 1)
    assert any(p.startswith("H-10") for p in problems)
    assert any(p.startswith("H-06") for p in problems)
