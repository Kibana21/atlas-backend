"""make doctor is read-only (B-08, FR-35, AC-04, AC-07)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from atlas.core.settings import SettingsError
from atlas.tools import doctor
from atlas.tools.doctor import PgReader, ReadOnlyViolationError, RedisReader, Report, Targets, effective_auth_method
from tests.conftest import make_settings

pytestmark = pytest.mark.unit
ALLOWED_REDIS = {"ping", "info", "dbsize", "scan"}


class RecordingCursor:
    def __init__(self, answers: dict[str, Any], fail_on: str | None = None) -> None:
        self.answers, self.fail_on = answers, fail_on
        self.sql: list[str] = []
        self._last: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> None:
        self.sql.append(query)
        if self.fail_on and self.fail_on in query:
            raise PermissionError("permission denied")
        self._last = next((v for k, v in self.answers.items() if k in query), [])

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._last


class RecordingRedis:
    def __init__(self, keys: int = 0) -> None:
        self.calls: list[str] = []
        self.keys = keys

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AttributeError(f"doctor must not call Redis {name!r}")

    def ping(self) -> bool:
        self.calls.append("ping")
        return True

    def info(self, section: str) -> dict[str, Any]:
        self.calls.append("info")
        return {"redis_version": "7.4.9"} if section == "server" else {"db1": {"keys": 3}, "db10": {"keys": self.keys}}

    def dbsize(self) -> int:
        self.calls.append("dbsize")
        return self.keys

    def scan(self, cursor: int = 0, match: str | None = None, count: int | None = None) -> tuple[int, list[Any]]:
        self.calls.append("scan")
        assert match is not None
        assert match.startswith("atlas:")
        return 0, []


PG15 = {
    "server_version_num": [("150012",)],
    "pg_roles WHERE rolname = ANY": [],
    "datname = ANY": [],
    "pg_hba_file_rules": [("local", ["all"], ["all"], "trust"), ("host", ["all"], ["all"], "trust")],
    "datname = 'atlas_db'": [("x",)],
    "NOT datistemplate": [("atlas_db",), ("postgres",)],
    "rolname !~": [("someone",)],
    "pg_namespace": [],
}


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    cursors: list[RecordingCursor],
    redis_clients: list[RecordingRedis],
    answers: dict[str, Any] | None = None,
    fail_on: str | None = None,
) -> None:
    @contextmanager
    def fake_session(url: str) -> Iterator[PgReader]:
        cur = RecordingCursor(answers or PG15, fail_on)
        cursors.append(cur)
        yield PgReader(cur)

    def fake_redis(url: str) -> RedisReader:
        client = RecordingRedis()
        redis_clients.append(client)
        return RedisReader(client)

    monkeypatch.setattr(doctor, "pg_session", fake_session)
    monkeypatch.setattr(doctor, "_redis", fake_redis)


def _local_settings() -> Any:
    return make_settings(
        env="local",
        database_url="postgresql+psycopg://atlas_app:x@localhost:5432/atlas_db",
        migration_database_url="postgresql+psycopg://atlas_migrator:x@localhost:5432/atlas_db",
        db_app_role="atlas_app",
        redis_url="redis://localhost:6379/10",
        redis_key_prefix="atlas:local:",
    )


def test_before_setup_reports_missing_roles_and_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    cursors: list[RecordingCursor] = []
    clients: list[RecordingRedis] = []
    _patch(monkeypatch, cursors, clients)
    report = Report()
    snapshot = doctor.run(
        Targets(_local_settings(), "postgresql://localhost:5432/postgres", "redis://localhost:6379/11"), report
    )
    text = report.render()
    assert "PASS     postgres.version: server major 15" in text
    assert "Atlas roles missing" in text
    assert "postgres-local-setup.sql" in text
    assert "WARN     postgres.auth" in text
    assert "'trust'" in text
    assert "connection not attempted" in text  # no login as a non-existent role
    assert report.failed
    assert snapshot["redis"]["keyspace"] == {"db1": 3, "db10": 0}
    every_sql = [q for c in cursors for q in c.sql]
    assert every_sql
    assert all(q.lstrip().upper().startswith(("SELECT", "SHOW")) for q in every_sql)
    assert {call for c in clients for call in c.calls} <= ALLOWED_REDIS


def test_auth_method_deferred_when_rules_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [], [], fail_on="pg_hba_file_rules")
    report = Report()
    doctor.run(Targets(_local_settings(), "postgresql://localhost:5432/postgres", None), report)
    assert "DEFERRED postgres.auth" in report.render()
    assert "no active probe" in report.render()


def test_after_setup_everything_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    ready = {
        **PG15,
        "pg_roles WHERE rolname = ANY": [(r,) for r in doctor.ATLAS_ROLES],
        "datname = ANY": [("atlas_db",), ("atlas_test",)],
        "pg_namespace": [(1,)],
        "pg_hba_file_rules": [("host", ["atlas_db"], ["atlas_app"], "scram-sha-256")],
        "current_user": [("atlas_app", "atlas_db")],
    }
    _patch(monkeypatch, [], [], answers=ready)
    report = Report()
    doctor.run(Targets(_local_settings(), "postgresql://localhost:5432/postgres", None), report)
    assert not report.failed, report.render()
    assert "connected as atlas_app to atlas_db" in report.render()


def test_metadata_url_missing_is_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [], [])
    report = Report()
    doctor.run(Targets(_local_settings(), None, None), report)
    assert "DEFERRED postgres.metadata" in report.render()


def test_pg_reader_refuses_writes() -> None:
    reader = PgReader(RecordingCursor({}))
    for sql in [
        "CREATE ROLE x",
        "ALTER DATABASE atlas_db OWNER TO x",
        "DROP TABLE t",
        "INSERT INTO t VALUES (1)",
        "SELECT 1; DROP TABLE t",
        "GRANT ALL ON DATABASE atlas_db TO x",
        "SET ROLE postgres",
    ]:
        with pytest.raises(ReadOnlyViolationError):
            reader.rows(sql)


def test_redis_reader_exposes_no_write_commands() -> None:
    public = {n for n in dir(RedisReader) if not n.startswith("_")}
    assert public == {"ping", "info", "dbsize", "count_prefixed"}


def test_scan_is_bounded() -> None:
    class Endless(RecordingRedis):
        def scan(self, cursor: int = 0, match: str | None = None, count: int | None = None) -> tuple[int, list[Any]]:
            self.calls.append("scan")
            return 1, ["k"]

    client = Endless()
    assert RedisReader(client).count_prefixed("atlas:local:") == (doctor.MAX_SCAN_ITERATIONS, False)
    assert client.calls.count("scan") == doctor.MAX_SCAN_ITERATIONS


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ([("local", ["all"], ["all"], "trust"), ("host", ["all"], ["all"], "trust")], "trust"),
        (
            [("host", ["atlas_db"], ["atlas_app"], "scram-sha-256"), ("host", ["all"], ["all"], "trust")],
            "scram-sha-256",
        ),
        ([("host", ["other"], ["all"], "reject"), ("host", ["all"], ["atlas_app"], "md5")], "md5"),
        ([("local", ["all"], ["all"], "trust")], None),
    ],
)
def test_effective_auth_method_first_match(rules: list[tuple[Any, ...]], expected: str | None) -> None:
    assert effective_auth_method(rules, database="atlas_db", role="atlas_app") == expected


def test_main_reports_invalid_settings(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def bad() -> Any:
        raise SettingsError("test runs must use database 'atlas_test'")

    monkeypatch.setattr(doctor, "get_settings", bad)
    assert doctor.main([]) == 1
    assert "FAIL     config: settings invalid" in capsys.readouterr().out


def test_main_refuses_non_atlas_redis_db(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prod = make_settings(
        env="prod",
        database_url="postgresql://h/atlas_db",
        migration_database_url="postgresql://h/atlas_db",
        db_app_role="atlas_app",
        redis_url="redis://r:6379/1",
        redis_key_prefix="atlas:prod:",
        diagnostics_enabled=False,
    )
    monkeypatch.setattr(doctor, "get_settings", lambda: prod)
    assert doctor.main([]) == 1
    assert "only inspects Atlas Redis DBs 10/11" in capsys.readouterr().out


def test_main_writes_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    _patch(monkeypatch, [], [])
    monkeypatch.setattr(doctor, "get_settings", _local_settings)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ATLAS_DOCTOR_METADATA_URL=postgresql://localhost:5432/postgres\n")
    (tmp_path / ".env.test").write_text("ATLAS_REDIS_URL=redis://localhost:6379/11\n")
    out = tmp_path / "snap.json"
    assert doctor.main(["--snapshot", str(out)]) == 1  # roles missing before P-03 -> FAIL, by design
    data = json.loads(out.read_text())
    assert data["postgres"]["server_major"] == 15
    assert set(data["redis"]) == {"local", "keyspace", "test"}
    assert "snapshot written" in capsys.readouterr().out


def test_connectivity_failures_are_reported_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def refused(url: str) -> Iterator[PgReader]:
        raise ConnectionRefusedError("refused")
        yield  # pragma: no cover

    def no_redis(url: str) -> RedisReader:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(doctor, "pg_session", refused)
    monkeypatch.setattr(doctor, "_redis", no_redis)
    report = Report()
    doctor.run(Targets(_local_settings(), "postgresql://localhost:5432/postgres", None), report)
    text = report.render()
    assert "FAIL     postgres.metadata: cannot open read-only metadata session (ConnectionRefusedError)" in text
    assert "FAIL     redis: unreachable (ConnectionRefusedError)" in text


def test_helpers(tmp_path: Any) -> None:
    assert doctor._with_database("postgresql://h:5432/postgres", "atlas_db") == "postgresql://h:5432/atlas_db"
    env_test = tmp_path / ".env.test"
    assert doctor._test_redis_url(env_test) is None
    env_test.write_text("ATLAS_REDIS_URL=redis://h:6379/10\n")
    assert doctor._test_redis_url(env_test) is None  # only DB 11 is accepted as the test DB
    env_test.write_text("OTHER=1\nATLAS_REDIS_URL=redis://h:6379/11\n")
    assert doctor._test_redis_url(env_test) == "redis://h:6379/11"


def _ingest_answers(**overrides: Any) -> dict[str, Any]:
    ready = {
        **PG15,
        "pg_roles WHERE rolname = ANY": [(r,) for r in doctor.ATLAS_ROLES],
        "pg_roles r WHERE": [("atlas_ingest", True, False, 2), ("atlas_test_ingest", True, False, 2)],
        "datname = ANY": [("atlas_db",), ("atlas_test",)],
        "pg_namespace": [(1,)],
        "role_table_grants": [("entity", "INSERT"), ("entity", "SELECT")],
        "current_user": [("atlas_app", "atlas_db")],
    }
    return {**ready, **overrides}


class PrivilegeCursor(RecordingCursor):
    """has_database_privilege answers by (role, database): CONNECT only to the role's own database."""

    def execute(self, query: str, params: Sequence[object] | None = None) -> None:
        super().execute(query, params)
        if "has_database_privilege" in query and params:
            role, database = str(params[0]), str(params[1])
            self._last = [(doctor.INGEST_ROLES.get(role) == database, False)]


def _patch_privileges(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> None:
    @contextmanager
    def fake_session(url: str) -> Iterator[PgReader]:
        yield PgReader(PrivilegeCursor(answers))

    monkeypatch.setattr(doctor, "pg_session", fake_session)
    monkeypatch.setattr(doctor, "_redis", lambda url: RedisReader(RecordingRedis()))


def test_ingest_roles_deferred_before_gate_g3r(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_privileges(monkeypatch, _ingest_answers(**{"pg_roles r WHERE": []}))
    report = Report()
    doctor.run(Targets(_local_settings(), "postgresql://localhost:5432/postgres", None), report)
    assert "DEFERRED postgres.ingest_roles: ingestion roles not created yet" in report.render()
    assert "g3r-runbook.md" in report.render()


def test_ingest_roles_pass_after_gate_g3r(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_privileges(monkeypatch, _ingest_answers())
    report = Report()
    snapshot = doctor.run(Targets(_local_settings(), "postgresql://localhost:5432/postgres", None), report)
    assert "PASS     postgres.ingest_roles: both ingestion roles present" in report.render()
    assert snapshot["postgres"]["ingest_roles"]["present"] == ["atlas_ingest", "atlas_test_ingest"]


def test_ingest_role_problems_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = _ingest_answers(
        **{
            "pg_roles r WHERE": [("atlas_ingest", True, True, 5)],
            "role_table_grants": [("entity", "DELETE")],
        }
    )
    _patch_privileges(monkeypatch, answers)
    report = Report()
    doctor.run(Targets(_local_settings(), "postgresql://localhost:5432/postgres", None), report)
    text = report.render()
    assert "FAIL     postgres.ingest_roles" in text
    assert "atlas_test_ingest missing" in text
    assert "CONNECTION LIMIT 2" in text
    assert "entity:DELETE" in text


def _single_settings() -> Any:
    url = "postgresql+psycopg://atlas_db_user@localhost:5432/atlas_db"
    return make_settings(
        env="local", database_url=url, migration_database_url=url, db_app_role="atlas_db_user",
        db_role_model="single", redis_url="redis://localhost:6379/10", redis_key_prefix="atlas:local:",
    )  # fmt: skip


def test_single_user_model_warns_about_a_superuser(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_privileges(monkeypatch, _ingest_answers(**{"pg_roles u WHERE": [(True, True)], "pg_roles r WHERE": []}))
    report = Report()
    snapshot = doctor.run(Targets(_single_settings(), "postgresql://localhost:5432/postgres", None), report)
    text = report.render()
    assert "WARN     postgres.role_model: single-user model" in text
    assert "SUPERUSER" in text
    assert "postgres.ingest_roles" not in text  # not used in the single-user model
    assert "PASS     postgres.runtime_role: connected as atlas_app" in text  # the fake answers as before
    assert snapshot["postgres"]["single_user"]["superuser"] is True


def test_single_user_model_reports_a_missing_or_plain_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_privileges(monkeypatch, _ingest_answers(**{"pg_roles u WHERE": []}))
    report = Report()
    doctor.run(Targets(_single_settings(), "postgresql://localhost:5432/postgres", None), report)
    assert "FAIL     postgres.role_model: single-user model: role 'atlas_db_user' does not exist" in report.render()
    _patch_privileges(monkeypatch, _ingest_answers(**{"pg_roles u WHERE": [(False, False)]}))
    report = Report()
    doctor.run(Targets(_single_settings(), "postgresql://localhost:5432/postgres", None), report)
    assert (
        "PASS     postgres.role_model: single-user model: every Atlas connection uses 'atlas_db_user'"
        in report.render()
    )
