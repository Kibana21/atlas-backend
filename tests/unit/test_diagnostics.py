"""GET /api/v1/diagnostics/build and build provenance (FR-27, AC-09)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas import version
from atlas.main import create_app
from tests.conftest import FakeDatabase, make_settings

pytestmark = pytest.mark.unit
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_enabled_returns_build_info() -> None:
    r = TestClient(create_app(make_settings(build_sha=SHA))).get("/api/v1/diagnostics/build")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "backend_sha": SHA,
        "specs_sha": "bfe4a09e5d7d260afe15576b4fb9fd7530949370",
        "contract_version": "0.1.0",
        "db_migration_revision": None,
        "environment": "test",
    }


def test_disabled_returns_not_found_envelope() -> None:
    r = TestClient(create_app(make_settings(diagnostics_enabled=False))).get("/api/v1/diagnostics/build")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert r.json()["error"]["correlation_id"] == r.headers["x-correlation-id"]


def test_missing_specs_lock_is_a_misconfiguration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(version, "SPECS_LOCK", tmp_path / "absent.lock")
    monkeypatch.setattr(
        "atlas.api.routers.diagnostics.read_specs_lock", lambda: version.read_specs_lock(tmp_path / "absent.lock")
    )
    r = TestClient(create_app(make_settings()), raise_server_exceptions=False).get("/api/v1/diagnostics/build")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"


def test_backend_sha_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    version.backend_sha.cache_clear()
    assert version.backend_sha(SHA) == SHA  # explicit build SHA wins
    assert version.backend_sha("not-a-sha") in {"unknown"} | {version.backend_sha(None)}
    version.backend_sha.cache_clear()
    local = version.backend_sha(None)  # this checkout's HEAD
    assert local == "unknown" or version.FULL_SHA.fullmatch(local)
    version.backend_sha.cache_clear()
    monkeypatch.setattr("atlas.version.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert version.backend_sha(None) == "unknown"
    version.backend_sha.cache_clear()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (f'specs_commit: "{SHA}"\ncontract_version: "0.1.0"\n', version.SpecsPin(SHA, "0.1.0")),
        ('specs_commit: "main"\ncontract_version: "0.1.0"\n', None),
        (f'specs_commit: "{SHA}"\n', None),
        ("# only a comment\n", None),
    ],
)
def test_read_specs_lock(tmp_path: Path, content: str, expected: version.SpecsPin | None) -> None:
    lock = tmp_path / "specs.lock"
    lock.write_text(content)
    assert version.read_specs_lock(lock) == expected


def test_repository_lock_pins_the_contract_release() -> None:
    pin = version.read_specs_lock()
    assert pin == version.SpecsPin("bfe4a09e5d7d260afe15576b4fb9fd7530949370", "0.1.0")
    assert version.contract_version() == "0.1.0"


def test_contract_version_falls_back_when_unpinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(version, "SPECS_LOCK", tmp_path / "absent.lock")
    monkeypatch.setattr(version, "read_specs_lock", lambda path=tmp_path / "absent.lock": None)
    assert version.contract_version() == version.UNPINNED_VERSION


def test_reports_the_migration_revision_read_by_the_runtime_role() -> None:
    app = create_app(make_settings(build_sha=SHA), database=FakeDatabase(revision="0001_baseline"))
    assert TestClient(app).get("/api/v1/diagnostics/build").json()["db_migration_revision"] == "0001_baseline"
