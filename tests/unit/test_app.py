"""Application factory conventions (FR-22, FR-38, AC-05, AC-12)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from atlas.main import create_app
from tests.conftest import make_settings

pytestmark = pytest.mark.unit

LOCAL = {
    "env": "local",
    "database_url": "postgresql+psycopg://atlas_app:x@localhost:5432/atlas_db",
    "migration_database_url": "postgresql+psycopg://atlas_migrator:x@localhost:5432/atlas_db",
    "db_app_role": "atlas_app",
    "redis_url": "redis://localhost:6379/10",
    "redis_key_prefix": "atlas:local:",
}
PROD = {
    **LOCAL,
    "env": "prod",
    "redis_url": "redis://r:6379/0",
    "redis_key_prefix": "atlas:prod:",
    "diagnostics_enabled": False,
}
SECURITY = {"x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "cache-control": "no-store"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_settings()))


def test_health_live(client: TestClient) -> None:
    r = client.get("/api/v1/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["x-correlation-id"].startswith("req_")
    for name, value in SECURITY.items():
        assert r.headers[name] == value


def test_cors_allowlist(client: TestClient) -> None:
    ok = client.get("/api/v1/health/live", headers={"Origin": "http://localhost:3000"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "x-correlation-id" in ok.headers["access-control-expose-headers"].lower()
    denied = client.get("/api/v1/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


def test_cors_preflight_carries_correlation_and_security_headers(client: TestClient) -> None:
    r = client.options(
        "/api/v1/health/live",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200
    assert r.headers["x-correlation-id"].startswith("req_")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_docs_only_in_local() -> None:
    assert TestClient(create_app(make_settings(**LOCAL))).get("/docs").status_code == 200
    assert TestClient(create_app(make_settings(**LOCAL))).get("/openapi.json").status_code == 200
    test_client = TestClient(create_app(make_settings()))
    assert test_client.get("/docs").status_code == 404
    assert test_client.get("/openapi.json").json()["error"]["code"] == "NOT_FOUND"


def test_openapi_is_identical_in_every_environment() -> None:
    docs = [json.dumps(create_app(make_settings(**o)).openapi(), sort_keys=True) for o in ({}, LOCAL, PROD)]
    assert docs[0] == docs[1] == docs[2]


def test_openapi_conventions() -> None:
    spec = create_app(make_settings()).openapi()
    assert spec["openapi"].startswith("3.1")
    ops = [op for item in spec["paths"].values() for op in item.values()]
    assert {op["operationId"] for op in ops} >= {"getHealthLive"}
    assert all(op["operationId"][0].islower() and "_" not in op["operationId"] for op in ops)
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert spec["components"]["headers"]["X-Correlation-ID"]["schema"]["pattern"] == "^[A-Za-z0-9_-]{8,64}$"
    for op in ops:
        assert "500" in op["responses"]
        for response in op["responses"].values():
            assert response["headers"]["X-Correlation-ID"] == {"$ref": "#/components/headers/X-Correlation-ID"}


def test_openapi_is_cached() -> None:
    app = create_app(make_settings())
    assert app.openapi() is app.openapi()
