"""GET /api/v1/health/ready database check (FR-26, B-09; Redis check lands in B-13)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from atlas.main import create_app
from tests.conftest import FakeDatabase, make_settings

pytestmark = pytest.mark.unit


def test_ready_when_database_answers() -> None:
    with TestClient(create_app(make_settings(), database=FakeDatabase())) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok"}}
    assert r.headers["x-correlation-id"]


def test_unavailable_database_is_a_503_envelope_without_connection_details() -> None:
    settings = make_settings()
    r = TestClient(create_app(settings, database=FakeDatabase(available=False))).get("/api/v1/health/ready")
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["details"] == {"failed_check": "database"}
    assert error["recoverable"] is True
    assert error["correlation_id"] == r.headers["x-correlation-id"]
    body = json.dumps(r.json())
    for fragment in ("localhost", "5432", "atlas_test_app", "postgresql"):
        assert fragment not in body


def test_ready_stays_out_of_the_schema_while_pending() -> None:
    # contract-pending.yaml lists getHealthReady until B-13/B-18 complete it (checks.redis).
    paths = create_app(make_settings()).openapi()["paths"]
    assert "/api/v1/health/ready" not in paths


def test_engine_is_disposed_on_shutdown() -> None:
    fake = FakeDatabase()
    with TestClient(create_app(make_settings(), database=fake)):
        assert not fake.disposed
    assert fake.disposed
