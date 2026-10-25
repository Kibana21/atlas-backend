"""Read-only local preflight (ATLAS-001 FR-35, B-08; owner restrictions of the CP2 authorization).

Checks the owner's existing PostgreSQL 15 and Redis without changing them:
  * PostgreSQL: only SELECT/SHOW, in a session with default_transaction_read_only=on.
  * Redis: only PING, INFO, DBSIZE and bounded SCAN.
  * No active authentication probe (no wrong-password attempts). The authentication method is read
    from pg_hba_file_rules when the metadata session may read it; otherwise it is reported DEFERRED.
  * A runtime-role connection is attempted only if that role already exists.

Usage: python -m atlas.tools.doctor [--snapshot FILE]
Exit status: 0 when nothing FAILs, 1 otherwise (WARN/DEFERRED never fail).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from atlas.core.settings import (
    LOCAL_REDIS_DB,
    TEST_DATABASE,
    TEST_REDIS_DB,
    Settings,
    SettingsError,
    database_name,
    get_settings,
    redis_db,
)

ATLAS_ROLES = ("atlas_migrator", "atlas_app", "atlas_test_migrator", "atlas_test_app")
ATLAS_DATABASES = ("atlas_db", TEST_DATABASE)
# Ingestion roles (ATLAS-002, DEC-044 as amended): created by the owner at gate G3-R, each for its own database.
INGEST_ROLES = {"atlas_ingest": "atlas_db", "atlas_test_ingest": TEST_DATABASE}
G3R_HINT = "owner gate G3-R: atlas-specs features/ATLAS-002-canonical-graph-foundation/proposals/g3r-runbook.md"
INGEST_TABLE_PRIVILEGES = frozenset({"SELECT", "INSERT"})  # UPDATE only per column (revision_to, last)
SETUP_HINT = "see atlas-specs features/ATLAS-001-engineering-foundation/proposals/postgres-local-setup.sql (P-03)"
READ_ONLY_SQL = re.compile(r"^\s*(SELECT|SHOW)\b", re.I)
MAX_SCAN_ITERATIONS = 10


class ReadOnlyViolationError(RuntimeError):
    pass


# --------------------------------------------------------------------------- read-only adapters
class Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] | None = ...) -> object: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...


class PgReader:
    """Executes only SELECT/SHOW statements (no DML/DDL can pass)."""

    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor

    def rows(self, sql: str, params: Sequence[object] | None = None) -> list[tuple[Any, ...]]:
        if not READ_ONLY_SQL.match(sql) or ";" in sql.strip().rstrip(";"):
            raise ReadOnlyViolationError(f"refusing non-read-only SQL: {sql.split()[0] if sql.split() else sql!r}")
        self._cursor.execute(sql, params)
        return self._cursor.fetchall()


class RedisLike(Protocol):
    def ping(self) -> object: ...
    def info(self, section: str) -> dict[str, Any]: ...
    def dbsize(self) -> int: ...
    def scan(self, cursor: int = ..., match: str | None = ..., count: int | None = ...) -> tuple[int, list[Any]]: ...


class RedisReader:
    """Exposes only PING, INFO, DBSIZE and bounded SCAN."""

    def __init__(self, client: RedisLike) -> None:
        self._client = client

    def ping(self) -> bool:
        return bool(self._client.ping())

    def info(self, section: str) -> dict[str, Any]:
        return self._client.info(section)

    def dbsize(self) -> int:
        return int(self._client.dbsize())

    def count_prefixed(self, prefix: str) -> tuple[int, bool]:
        """Count keys under prefix with at most MAX_SCAN_ITERATIONS SCAN calls; (count, complete)."""
        cursor, total = 0, 0
        for _ in range(MAX_SCAN_ITERATIONS):
            cursor, keys = self._client.scan(cursor=cursor, match=f"{prefix}*", count=100)
            total += len(keys)
            if cursor == 0:
                return total, True
        return total, False


# --------------------------------------------------------------------------- results
@dataclass
class Report:
    lines: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, status: str, check: str, detail: str) -> None:
        self.lines.append((status, check, detail))

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _, _ in self.lines)

    def render(self) -> str:
        return "\n".join(f"{s:8s} {c}: {d}" for s, c, d in self.lines)


# --------------------------------------------------------------------------- checks
def check_postgres(reader: PgReader, report: Report, db_reader: PgReader | None) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    (version_num,) = reader.rows("SHOW server_version_num")[0]
    major = int(version_num) // 10000
    facts["server_major"] = major
    report.add("PASS" if major == 15 else "FAIL", "postgres.version", f"server major {major} (approved baseline: 15)")

    roles = {r for (r,) in reader.rows("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ATLAS_ROLES),))}
    missing_roles = [r for r in ATLAS_ROLES if r not in roles]
    facts["atlas_roles_present"] = sorted(roles)
    report.add(
        "PASS" if not missing_roles else "FAIL",
        "postgres.roles",
        "all four Atlas roles present"
        if not missing_roles
        else f"Atlas roles missing: {', '.join(missing_roles)} -> {SETUP_HINT}",
    )

    dbs = {
        d for (d,) in reader.rows("SELECT datname FROM pg_database WHERE datname = ANY(%s)", (list(ATLAS_DATABASES),))
    }
    facts["atlas_databases_present"] = sorted(dbs)
    missing_dbs = [d for d in ATLAS_DATABASES if d not in dbs]
    report.add(
        "PASS" if not missing_dbs else "FAIL",
        "postgres.databases",
        "atlas_db and atlas_test present" if not missing_dbs else f"missing: {', '.join(missing_dbs)} -> {SETUP_HINT}",
    )

    if db_reader is not None:
        has_schema = bool(db_reader.rows("SELECT 1 FROM pg_namespace WHERE nspname = 'atlas'"))
        report.add(
            "PASS" if has_schema else "FAIL",
            "postgres.schema",
            "schema atlas present in atlas_db" if has_schema else f"schema atlas missing in atlas_db -> {SETUP_HINT}",
        )

    facts["ingest_roles"] = check_ingest_roles(reader, report, db_reader)

    try:
        rules = reader.rows(
            "SELECT type, database, user_name, auth_method FROM pg_hba_file_rules"
            " WHERE error IS NULL ORDER BY line_number"
        )
    except ReadOnlyViolationError:
        raise
    except Exception:
        report.add(
            "DEFERRED",
            "postgres.auth",
            "pg_hba_file_rules not readable by the metadata session; no active probe run (A4)",
        )
        return facts
    method = effective_auth_method(rules, database="atlas_db", role="atlas_app")
    facts["loopback_auth_method_for_atlas_app"] = method
    if method == "trust":
        report.add(
            "WARN", "postgres.auth", "loopback connections use 'trust': passwords are not verified locally (DEC-084)"
        )
    elif method is None:
        report.add("DEFERRED", "postgres.auth", "no matching host rule found in pg_hba_file_rules")
    else:
        report.add("PASS", "postgres.auth", f"loopback connections for atlas_app use {method!r}")
    return facts


def check_ingest_roles(reader: PgReader, report: Report, db_reader: PgReader | None) -> dict[str, Any]:
    """Presence, attributes and isolation of the ingestion roles; no table privilege beyond the DEC-044 matrix."""
    rows = reader.rows(
        "SELECT r.rolname, r.rolcanlogin, r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication "
        "OR r.rolbypassrls, r.rolconnlimit FROM pg_roles r WHERE r.rolname = ANY(%s) ORDER BY 1",
        (sorted(INGEST_ROLES),),
    )
    present = {name: (login, elevated, limit) for name, login, elevated, limit in rows}
    if not present:
        report.add("DEFERRED", "postgres.ingest_roles", f"ingestion roles not created yet -> {G3R_HINT}")
        return {"present": []}
    problems = [f"{name} missing" for name in INGEST_ROLES if name not in present]
    for name, (login, elevated, limit) in sorted(present.items()):
        if not login or elevated or limit != 2:
            problems.append(f"{name}: expected LOGIN, no special attributes, CONNECTION LIMIT 2")
        for database in ATLAS_DATABASES:
            (connect, temp) = reader.rows(
                "SELECT has_database_privilege(%s, %s, 'CONNECT'), has_database_privilege(%s, %s, 'TEMP')",
                (name, database, name, database),
            )[0]
            if bool(connect) != (database == INGEST_ROLES[name]) or temp:
                problems.append(f"{name}: CONNECT must be on {INGEST_ROLES[name]} only, and never TEMP")
    excess: list[str] = []
    if db_reader is not None and "atlas_ingest" in present:
        grants = db_reader.rows(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'atlas_ingest' ORDER BY 1, 2"
        )
        excess = [f"{t}:{p}" for t, p in grants if p not in INGEST_TABLE_PRIVILEGES]
        if excess:
            problems.append(f"atlas_ingest holds privileges beyond the matrix: {', '.join(excess)}")
    report.add(
        "FAIL" if problems else "PASS",
        "postgres.ingest_roles",
        "; ".join(problems) if problems else "both ingestion roles present, isolated, no excess table privilege",
    )
    return {"present": sorted(present), "excess_privileges": excess}


def effective_auth_method(rules: Iterable[tuple[Any, ...]], *, database: str, role: str) -> str | None:
    """First host rule that applies to (database, role) over TCP, like PostgreSQL's first match."""
    for rule_type, dbs, users, method in rules:
        if rule_type not in ("host", "hostssl", "hostnossl"):
            continue
        if ("all" in dbs or database in dbs) and ("all" in users or role in users):
            return str(method)
    return None


def check_redis(name: str, reader: RedisReader, expected_db: int, prefix: str, report: Report) -> dict[str, Any]:
    reader.ping()
    version = reader.info("server").get("redis_version", "unknown")
    size = reader.dbsize()
    atlas_keys, complete = reader.count_prefixed(prefix)
    report.add(
        "PASS",
        f"redis.{name}",
        f"reachable (Redis {version}); DB {expected_db} holds {size} keys, "
        f"{atlas_keys}{'' if complete else '+'} under {prefix!r}",
    )
    return {"redis_version": version, "db": expected_db, "dbsize": size, "atlas_prefixed_keys": atlas_keys}


def redis_keyspace(reader: RedisReader) -> dict[str, int]:
    return {
        db: int(stats.get("keys", 0))
        for db, stats in sorted(reader.info("keyspace").items())
        if isinstance(stats, dict)
    }


# --------------------------------------------------------------------------- wiring
@dataclass
class Targets:
    settings: Settings
    metadata_url: str | None
    test_redis_url: str | None


@contextmanager
def pg_session(url: str) -> Iterator[PgReader]:
    import psycopg

    conninfo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with (
        psycopg.connect(
            conninfo, connect_timeout=3, options="-c default_transaction_read_only=on", autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        yield PgReader(cur)


def _redis(url: str) -> RedisReader:
    import redis

    return RedisReader(redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2))


def run(targets: Targets, report: Report) -> dict[str, Any]:
    s = targets.settings
    snapshot: dict[str, Any] = {"environment": s.env.value}
    report.add(
        "PASS",
        "config",
        f"settings valid for {s.env.value} "
        f"(database {database_name(s.database_url)!r}, Redis DB {redis_db(s.redis_url)})",
    )
    roles_present: list[str] = []
    if targets.metadata_url:
        try:
            with pg_session(targets.metadata_url) as reader:
                has_atlas_db = bool(reader.rows("SELECT 1 FROM pg_database WHERE datname = 'atlas_db'"))
                if has_atlas_db:
                    with pg_session(_with_database(targets.metadata_url, "atlas_db")) as db_reader:
                        facts = check_postgres(reader, report, db_reader)
                else:
                    facts = check_postgres(reader, report, None)
                roles_present = facts["atlas_roles_present"]
                snapshot["postgres"] = facts | {
                    "databases": sorted(
                        d for (d,) in reader.rows("SELECT datname FROM pg_database WHERE NOT datistemplate")
                    ),
                    "roles": sorted(r for (r,) in reader.rows("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_'")),
                }
        except ReadOnlyViolationError:
            raise
        except Exception as exc:
            report.add("FAIL", "postgres.metadata", f"cannot open read-only metadata session ({type(exc).__name__})")
    else:
        report.add("DEFERRED", "postgres.metadata", "ATLAS_DOCTOR_METADATA_URL not set; server checks skipped")

    app_role = s.db_app_role
    if app_role in roles_present:
        try:
            with pg_session(s.database_url) as reader:
                user, db = reader.rows("SELECT current_user, current_database()")[0]
            report.add("PASS", "postgres.runtime_role", f"connected as {user} to {db} (read-only session)")
        except Exception as exc:
            report.add("FAIL", "postgres.runtime_role", f"cannot connect as {app_role} ({type(exc).__name__})")
    else:
        report.add(
            "FAIL",
            "postgres.runtime_role",
            f"role {app_role!r} does not exist yet; connection not attempted -> {SETUP_HINT}",
        )

    try:
        local = _redis(s.redis_url)
        snapshot["redis"] = {
            "local": check_redis("local", local, redis_db(s.redis_url), s.redis_key_prefix, report),
            "keyspace": redis_keyspace(local),
        }
        if targets.test_redis_url:
            snapshot["redis"]["test"] = check_redis(
                "test", _redis(targets.test_redis_url), TEST_REDIS_DB, "atlas:test:", report
            )
    except Exception as exc:
        report.add("FAIL", "redis", f"unreachable ({type(exc).__name__})")
    return snapshot


def _with_database(url: str, database: str) -> str:
    head, _, _ = url.rpartition("/")
    return f"{head}/{database}"


def _test_redis_url(env_file: Path) -> str | None:
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("ATLAS_REDIS_URL="):
            url = line.split("=", 1)[1].strip()
            return url if redis_db(url) == TEST_REDIS_DB else None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Atlas local preflight")
    parser.add_argument("--snapshot", type=Path, help="write a read-only inventory to this JSON file")
    args = parser.parse_args(argv)
    report = Report()
    try:
        settings = get_settings()
    except (SettingsError, ValueError) as exc:
        report.add("FAIL", "config", f"settings invalid: {exc}")
        print(report.render())
        return 1
    if redis_db(settings.redis_url) not in (LOCAL_REDIS_DB, TEST_REDIS_DB):
        report.add("FAIL", "config", "doctor only inspects Atlas Redis DBs 10/11")
        print(report.render())
        return 1
    targets = Targets(
        settings,
        os.environ.get("ATLAS_DOCTOR_METADATA_URL") or _dotenv_value("ATLAS_DOCTOR_METADATA_URL"),
        _test_redis_url(Path(".env.test")),
    )
    snapshot = run(targets, report)
    print(report.render())
    if args.snapshot:
        args.snapshot.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"snapshot written to {args.snapshot}")
    return 1 if report.failed else 0


def _dotenv_value(key: str) -> str | None:
    env_file = Path(os.environ.get("ATLAS_ENV_FILE", ".env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or None
    return None


if __name__ == "__main__":
    sys.exit(main())
