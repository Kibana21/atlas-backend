"""F-09: the canonical loader CLI as a real process (``python -m atlas.tools.canonical``) on atlas_test only.

Every test runs the module entry point in a subprocess with settings built from the validated test settings, so the
FR-21 guards (database ``atlas_test`` for every role) apply to the CLI too. The runtime and migration URLs given to the
CLI point at an unreachable port: a command can only succeed through the ingestion connection, and the server-set
``change_set.db_role`` must be the ingestion role.

The CLI commits, so each test gets its own organization, created as the migration connection and deleted with all its
canonical rows afterwards (the golden fixture's synthetic, test-only taxonomy overlay is registered for the module).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from atlas.core.ids import uuid7
from atlas.core.settings import TEST_DATABASE, Settings, database_name
from atlas.persistence.canonical import history, integrity, repository
from atlas.persistence.migrations import metadata
from tests.integration.conftest import connect
from tests.integration.test_canonical_cp4 import FIXTURE, golden

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
BATCH = FIXTURE / "inputs" / "aws-inventory.r2.json"
ORG_TABLES = [t for t in reversed(metadata.sorted_tables) if "org_id" in t.c]  # children first
UNREACHABLE_PORT = 1


def _unreachable(url: str) -> str:
    return make_url(url).set(host="127.0.0.1", port=UNREACHABLE_PORT).render_as_string(hide_password=False)


def cli_env(settings: Settings, **overrides: str | None) -> dict[str, str]:
    """The CLI's environment: test settings with only the ingestion URL reachable; ``None`` removes a variable."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    ingest_url, ingest_role = settings.ingestion_url, settings.ingestion_role
    assert ingest_url is not None
    assert ingest_role is not None
    values: dict[str, str | None] = {
        "ATLAS_ENV_FILE": str(ROOT / "nonexistent.env"),
        "ATLAS_ENV": settings.env.value,
        "ATLAS_DB_ROLE_MODEL": settings.db_role_model,
        "ATLAS_DATABASE_URL": _unreachable(settings.database_url),
        "ATLAS_MIGRATION_DATABASE_URL": _unreachable(settings.migration_database_url),
        "ATLAS_DB_APP_ROLE": settings.db_app_role,
        "ATLAS_INGEST_DATABASE_URL": ingest_url,
        "ATLAS_DB_INGEST_ROLE": ingest_role,
        "ATLAS_REDIS_URL": settings.redis_url,
        "ATLAS_REDIS_KEY_PREFIX": settings.redis_key_prefix,
        **overrides,
    }
    env.update({k: v for k, v in values.items() if v is not None})
    return env


def run_cli(
    settings: Settings, *args: str, refused_database: bool = False, **overrides: str | None
) -> subprocess.CompletedProcess[str]:
    env = cli_env(settings, **overrides)
    urls = [v for k, v in env.items() if k.startswith("ATLAS_") and k.endswith("DATABASE_URL")]
    assert refused_database or {database_name(u) for u in urls} <= {TEST_DATABASE}  # never another database
    return subprocess.run(  # noqa: S603 - fixed interpreter and module, test-controlled arguments
        [sys.executable, "-m", "atlas.tools.canonical", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def ok(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr[-2000:]
    out: dict[str, Any] = json.loads(result.stdout)
    return out


def no_secrets(result: subprocess.CompletedProcess[str], settings: Settings) -> None:
    """Nothing the CLI prints names a connection: no URL, host, port or password."""
    text = result.stdout + result.stderr
    assert "Traceback" not in text
    for url in (settings.database_url, settings.migration_database_url, settings.ingestion_url):
        u = make_url(url or "")
        for secret in (u.password, f"{u.host}:{u.port}", "postgresql", "127.0.0.1"):
            assert not secret or str(secret) not in text


# --------------------------------------------------------------------------- organizations (created and removed)


@pytest.fixture(scope="module")
def overlay(migrated: None, settings: Settings) -> Iterator[None]:
    data = json.loads(next(FIXTURE.glob("taxonomy-overlay.*.json")).read_text(encoding="utf-8"))
    version = data["taxonomy_version"]
    with connect(settings.migration_database_url) as owner:
        owner.execute("INSERT INTO atlas.taxonomy_release (version, status) VALUES (%s, %s)", (version, data["status"]))
        for provider, items in data["add_native_type_mappings"].items():
            for m in items:
                owner.execute(
                    "INSERT INTO atlas.native_type_mapping VALUES (%s, %s, %s, %s)",
                    (version, provider, m["native_type"], m["category"]),
                )
    try:
        yield
    finally:
        with connect(settings.migration_database_url) as owner:
            owner.execute("DELETE FROM atlas.native_type_mapping WHERE taxonomy_version = %s", (version,))
            owner.execute("DELETE FROM atlas.taxonomy_release WHERE version = %s", (version,))


@pytest.fixture
def new_org(overlay: None, settings: Settings) -> Iterator[Callable[[str], str]]:
    """Creates organizations ``cli-<name>-<run>``; afterwards deletes them with every canonical row they own."""
    created: list[str] = []

    def make(name: str) -> str:
        oid, key = str(uuid7()), f"cli-{name}-{uuid7().hex[-8:]}"
        with connect(settings.migration_database_url) as owner:
            owner.execute("INSERT INTO atlas.organization (id, key, display_name) VALUES (%s, %s, %s)", (oid, key, key))
            owner.execute("INSERT INTO atlas.org_revision (org_id, last) VALUES (%s, 0)", (oid,))
        created.append(oid)
        return key

    yield make
    with connect(settings.migration_database_url) as owner, owner.transaction():
        for oid in created:
            for table in ORG_TABLES:
                owner.execute(
                    sql.SQL("DELETE FROM {} WHERE org_id = %s").format(sql.Identifier(table.schema or "", table.name)),
                    (oid,),
                )
            owner.execute("DELETE FROM atlas.organization WHERE id = %s", (oid,))
        left = owner.execute(
            "SELECT count(*) FROM atlas.organization WHERE id = ANY(%s::uuid[])", (created,)
        ).fetchone()
        assert left == (0,)


def observe(settings: Settings, key: str) -> tuple[repository.OrgScope, int, list[dict[str, Any]]]:
    """(scope, head revision, change sets with their server-set ``db_role``) read as the migration connection."""
    with connect(settings.migration_database_url) as conn:
        scope = repository.resolve_scope(conn, key)
        rows = conn.execute(
            "SELECT revision, kind, channel, actor_kind, actor_namespace, actor_key, reverts_revision, db_role "
            "FROM atlas.change_set WHERE org_id = %s ORDER BY revision",
            (scope.org_id,),
        ).fetchall()
        head = repository.current_revision(conn, scope)
    names = ("revision", "kind", "channel", "actor_kind", "actor_namespace", "actor_key", "reverts", "db_role")
    return scope, head, [dict(zip(names, r, strict=True)) for r in rows]


def expected_history() -> dict[int, dict[str, Any]]:
    rows = json.loads((FIXTURE / "expected-as-of.json").read_text(encoding="utf-8"))
    return {e["revision"]: e for e in rows if e["org_key"] == "default"}


@pytest.fixture
def replayed(new_org: Callable[[str], str], settings: Settings) -> tuple[str, dict[str, Any]]:
    """An organization loaded with the whole golden history (r1-r8) through ``replay``."""
    key = new_org("golden")
    out = ok(run_cli(settings, "replay", "--org", key, "--fixture", str(FIXTURE), "--source-org", "default"))
    return key, out


def library_replay(settings: Settings, key: str, until: int) -> None:
    """Golden history r1..``until`` through the library (committed), for commands that start from a later state."""
    ds, taxes, policy = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    ingest_url = settings.ingestion_url
    assert ingest_url is not None
    with psycopg.connect(repository.libpq_url(ingest_url)) as conn:
        history.replay(conn, repository.resolve_scope(conn, key), ds, source, taxes, policy, until=until)


@pytest.fixture
def approved_taxonomy_only(new_org: Callable[[str], str], settings: Settings) -> str:
    """Golden r1-r6: every version under the approved taxonomy 1.1.0 (the synthetic overlay arrives at r7)."""
    key = new_org("r6")
    library_replay(settings, key, until=6)
    return key


# --------------------------------------------------------------------------- successful operations


def test_replay_loads_the_golden_history_as_the_fixture_loader_over_the_ingestion_connection(
    replayed: tuple[str, dict[str, Any]], settings: Settings
) -> None:
    key, out = replayed
    expected = expected_history()
    assert out["command"] == "replay"
    assert [r["revision"] for r in out["revisions"]] == sorted(expected)
    assert [tuple(g) for g in out["graph"]] == [
        (expected[r]["entities"], expected[r]["relationships"], expected[r]["graph_hash"]) for r in sorted(expected)
    ]
    scope, head, change_sets = observe(settings, key)
    assert head == max(expected)
    assert {(c["actor_kind"], c["actor_namespace"], c["actor_key"]) for c in change_sets} == {
        ("service", "atlas.service", "fixture-loader")
    }
    assert {c["channel"] for c in change_sets} == {"fixture_cli"}
    assert {c["db_role"] for c in change_sets} == {settings.ingestion_role}  # session_user: the ingestion account
    with connect(settings.migration_database_url) as conn:
        assert all(integrity.revision_findings(conn, scope, r) == [] for r in range(1, head + 1))
    again = run_cli(settings, "replay", "--org", key, "--fixture", str(FIXTURE), "--source-org", "default")
    assert again.returncode == 2
    assert any("H-05" in f for f in json.loads(again.stdout)["rejected"])
    assert observe(settings, key)[1:] == (head, change_sets)  # the refused replay wrote nothing


def test_revert_is_a_new_system_attributed_revision_and_a_conflicting_one_writes_nothing(
    approved_taxonomy_only: str, settings: Settings
) -> None:
    key = approved_taxonomy_only
    _, head, before = observe(settings, key)
    assert head == 6
    refused = run_cli(settings, "revert", "--org", key, "--revision", "4")  # r5 already reverted it
    assert refused.returncode == 2, refused.stderr[-2000:]
    findings = json.loads(refused.stdout)["rejected"]
    assert findings
    assert all(f.startswith("[DOMAIN] D-14: revert of r4 refused") for f in findings)  # changed again after r4
    assert observe(settings, key)[1:] == (head, before)
    out = ok(run_cli(settings, "revert", "--org", key, "--revision", "6"))
    assert (out["command"], out["revision"]) == ("revert", 7)
    assert out["closed"]  # the r6 versions are closed, nothing is deleted
    scope, new_head, after = observe(settings, key)
    assert after[:-1] == before
    assert after[-1] == {
        "revision": 7,
        "kind": "revert",
        "channel": "system_job",
        "actor_kind": "system",
        "actor_namespace": "atlas.system",
        "actor_key": "revert",
        "reverts": 6,
        "db_role": settings.ingestion_role,
    }
    expected = expected_history()
    with connect(settings.migration_database_url) as conn:
        assert history.hash_at(conn, scope, 7)[2] == expected[5]["graph_hash"]  # the r6 reclassification undone
        assert history.hash_at(conn, scope, 6)[2] == expected[6]["graph_hash"]  # and r6 still reconstructs
        assert integrity.revision_findings(conn, scope, new_head) == []


def test_recompute_trust_then_snapshot_the_current_revision(approved_taxonomy_only: str, settings: Settings) -> None:
    key = approved_taxonomy_only
    _, head, before = observe(settings, key)
    first = ok(run_cli(settings, "recompute-trust", "--org", key))
    assert first["command"] == "recompute-trust"
    if first["revision"] is not None:  # the CLI evaluates now, without the fixture's synthetic freshness policy
        assert first["revision"] == head + 1
        cs = observe(settings, key)[2][-1]
        assert (cs["kind"], cs["channel"], cs["actor_kind"], cs["actor_key"], cs["db_role"]) == (
            "migration",
            "migration",
            "system",
            "trust-recompute",
            settings.ingestion_role,
        )
    second = ok(run_cli(settings, "recompute-trust", "--org", key))
    assert second["revision"] is None  # nothing changed: no change set
    scope, head, change_sets = observe(settings, key)
    assert len(change_sets) == len(before) + (first["revision"] is not None)

    snap = ok(run_cli(settings, "snapshot", "--org", key, "--label", "cli-baseline"))
    with connect(settings.migration_database_url) as conn:
        assert snap == {
            "command": "snapshot",
            "label": "cli-baseline",
            "revision": head,
            "graph_hash": history.hash_at(conn, scope, head)[2],
        }
        assert history.verify_snapshots(conn, scope) == []
    duplicate = run_cli(settings, "snapshot", "--org", key, "--label", "cli-baseline")  # a label never moves
    assert duplicate.returncode == 1
    assert duplicate.stderr.strip() == "atlas-canonical: snapshot label 'cli-baseline' already exists"
    with connect(settings.migration_database_url) as conn:
        count = conn.execute("SELECT count(*) FROM atlas.graph_snapshot WHERE org_id = %s", (scope.org_id,))
        assert count.fetchone() == (1,)
    assert observe(settings, key)[1:] == (head, change_sets)


def test_validating_commands_refuse_taxonomy_versions_they_have_not_loaded(
    replayed: tuple[str, dict[str, Any]], settings: Settings
) -> None:
    """r7 stamped the synthetic 1.2.0-test.1; the CLI loads only the approved taxonomy, so it must not validate."""
    key, _ = replayed
    _, head, change_sets = observe(settings, key)
    for args in (["revert", "--revision", "7"], ["revert", "--revision", "8"], ["recompute-trust"]):
        result = run_cli(settings, args[0], "--org", key, *args[1:])
        assert result.returncode == 1, (args, result.stdout[-2000:])
        assert result.stderr.strip() == (
            "atlas-canonical: taxonomy versions in use but not loaded by this command: 1.2.0-test.1"
        )
        assert observe(settings, key)[1:] == (head, change_sets)


def test_import_publishes_the_plan_as_the_import_worker_and_reports_rejected_items(
    new_org: Callable[[str], str], settings: Settings
) -> None:
    key = new_org("import")
    library_replay(settings, key, until=1)  # the CLI imports r2
    ds, _, _ = golden()
    source = next(o["id"] for o in ds.tables["organizations"] if o["key"] == "default")
    report = json.loads((FIXTURE / "inputs" / "aws-inventory.r2.expected-report.json").read_text(encoding="utf-8"))
    out = ok(run_cli(settings, "import", "--org", key, "--batch", str(BATCH)))
    assert (out["command"], out["revision"]) == ("import", 2)
    assert out["rejected"] == report["rejected"]  # rejected and reported, never stored
    scope, head, change_sets = observe(settings, key)
    assert head == 2
    assert {k: change_sets[-1][k] for k in ("kind", "channel", "actor_kind", "actor_key", "db_role")} == {
        "kind": "import",
        "channel": "import",
        "actor_kind": "service",
        "actor_key": "import-worker",
        "db_role": settings.ingestion_role,
    }
    with connect(settings.migration_database_url) as conn:
        created = {e["id"] for e in repository.load(conn, scope).tables["entities"] if e["created_revision"] == 2}
        assert integrity.revision_findings(conn, scope, 2) == []
    assert created == {e["id"] for e in ds.tables["entities"] if e["org_id"] == source and e["created_revision"] == 2}
    again = ok(run_cli(settings, "import", "--org", key, "--batch", str(BATCH)))
    assert again["revision"] is None  # the same batch again is a no-op
    assert observe(settings, key)[1:] == (head, change_sets)


# --------------------------------------------------------------------------- invalid arguments


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["unknown"],
        ["snapshot", "--label", "x"],  # --org missing
        ["revert", "--org", "x"],  # --revision missing
        ["revert", "--org", "x", "--revision", "seven"],
        ["replay", "--org", "x"],  # --fixture missing
        ["import", "--org", "x", "--batch"],
    ],
)
def test_invalid_arguments_exit_2_with_usage_before_any_configuration_is_read(
    settings: Settings, args: list[str]
) -> None:
    result = run_cli(settings, *args, ATLAS_ENV=None)  # no configuration at all: parsing fails first
    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage: atlas-canonical" in result.stderr


# --------------------------------------------------------------------------- configuration and failures


def test_missing_configuration_exits_1_without_echoing_any_value(settings: Settings) -> None:
    result = run_cli(
        settings,
        "snapshot",
        "--org",
        "x",
        "--label",
        "y",
        ATLAS_ENV=None,
        ATLAS_REDIS_URL=None,
        ATLAS_DB_APP_ROLE=None,
        ATLAS_DATABASE_URL=settings.database_url,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("atlas-canonical: invalid configuration: ")
    for field in ("env", "redis_url", "db_app_role"):
        assert f"{field}: Field required" in result.stderr
    no_secrets(result, settings)


def test_no_ingestion_connection_configured_exits_1(settings: Settings) -> None:
    result = run_cli(
        settings,
        "recompute-trust",
        "--org",
        "x",
        ATLAS_DB_ROLE_MODEL="separated",
        ATLAS_INGEST_DATABASE_URL=None,
        ATLAS_DB_INGEST_ROLE=None,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no ingestion connection configured" in result.stderr
    no_secrets(result, settings)


def test_test_settings_naming_another_database_are_refused_before_connecting(settings: Settings) -> None:
    """The FR-21 guard binds the CLI: ATLAS_ENV=test with atlas_db in any URL never connects (the port is closed)."""
    elsewhere = make_url(settings.ingestion_url or "").set(host="127.0.0.1", port=UNREACHABLE_PORT, database="atlas_db")
    result = run_cli(
        settings,
        "snapshot",
        "--org",
        "x",
        "--label",
        "y",
        refused_database=True,
        ATLAS_INGEST_DATABASE_URL=elsewhere.render_as_string(hide_password=False),
    )
    assert result.returncode == 1
    assert "test runs must use database 'atlas_test' for every role" in result.stderr
    no_secrets(result, settings)


def test_unreachable_ingestion_database_exits_1_without_connection_details(settings: Settings) -> None:
    result = run_cli(
        settings,
        "snapshot",
        "--org",
        "x",
        "--label",
        "y",
        ATLAS_INGEST_DATABASE_URL=_unreachable(settings.ingestion_url or ""),
    )
    assert result.returncode == 1
    assert result.stderr.startswith("atlas-canonical: ingestion database unavailable or operation failed")
    no_secrets(result, settings)


def test_unknown_organization_exits_1_and_writes_nothing(settings: Settings) -> None:
    key = f"cli-absent-{uuid7().hex[-8:]}"
    result = run_cli(settings, "snapshot", "--org", key, "--label", "y")
    assert result.returncode == 1
    assert result.stderr.strip() == f"atlas-canonical: organization not found: {key}"
    with connect(settings.migration_database_url) as conn:
        assert conn.execute("SELECT count(*) FROM atlas.organization WHERE key = %s", (key,)).fetchone() == (0,)
    no_secrets(result, settings)


def test_unreadable_input_exits_1_and_writes_nothing(new_org: Callable[[str], str], settings: Settings) -> None:
    key = new_org("input")
    missing = ROOT / "build" / "no-such-batch.json"
    result = run_cli(settings, "import", "--org", key, "--batch", str(missing))
    assert result.returncode == 1
    assert result.stderr.strip() == f"atlas-canonical: cannot read input: {missing}"
    assert observe(settings, key)[1:] == (0, [])
    no_secrets(result, settings)
