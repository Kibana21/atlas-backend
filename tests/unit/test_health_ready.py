"""GET /api/v1/health/ready (FR-26, AC-05): database and Redis, each bounded; 503 names the failed check."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from atlas.main import create_app
from atlas.persistence.redis import RedisUnavailable
from tests.conftest import FakeDatabase, FakeRedisProbe, make_settings

pytestmark = pytest.mark.unit
LEAKS = ("localhost", "5432", "6379", "atlas_test_app", "postgresql", "redis://")


def _ready(database: object, redis: object) -> tuple[int, dict[str, object], str]:
    app = create_app(make_settings(), database=database, redis=redis)  # type: ignore[arg-type]
    r = TestClient(app).get("/api/v1/health/ready")
    return r.status_code, r.json(), r.headers["x-correlation-id"]


def test_ready_when_both_dependencies_answer() -> None:
    status, body, cid = _ready(FakeDatabase(), FakeRedisProbe())
    assert (status, body) == (200, {"status": "ok", "checks": {"database": "ok", "redis": "ok"}})
    assert cid


@pytest.mark.parametrize(
    ("database", "redis", "failed"),
    [
        (FakeDatabase(available=False), FakeRedisProbe(), "database"),
        (FakeDatabase(), FakeRedisProbe(available=False), "redis"),
        (FakeDatabase(available=False), FakeRedisProbe(available=False), "database"),
    ],
)
def test_unavailable_dependency_is_a_503_envelope_naming_it(database: object, redis: object, failed: str) -> None:
    status, body, cid = _ready(database, redis)
    assert status == 503
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["details"] == {"failed_check": failed}
    assert error["recoverable"] is True
    assert error["correlation_id"] == cid
    text = json.dumps(body)
    assert not [fragment for fragment in LEAKS if fragment in text]


class _SlowRedis(FakeRedisProbe):
    async def ping(self) -> None:  # what AtlasRedis turns a timeout into
        await asyncio.sleep(0)
        raise RedisUnavailable


def test_checks_run_concurrently_and_unexpected_errors_are_internal_errors() -> None:
    class Broken(FakeRedisProbe):
        async def ping(self) -> None:
            raise RuntimeError("redis://user:pw@host:6379/11 exploded")

    app = create_app(make_settings(), database=FakeDatabase(), redis=Broken())
    r = TestClient(app, raise_server_exceptions=False).get("/api/v1/health/ready")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "exploded" not in r.text
    assert _ready(FakeDatabase(), _SlowRedis())[0] == 503


def test_ready_is_in_the_schema_with_the_contract_responses() -> None:
    operation = create_app(make_settings()).openapi()["paths"]["/api/v1/health/ready"]["get"]
    assert operation["operationId"] == "getHealthReady"
    assert set(operation["responses"]) == {"200", "503", "500"}


def test_clients_are_closed_on_shutdown() -> None:
    database, redis = FakeDatabase(), FakeRedisProbe()
    with TestClient(create_app(make_settings(), database=database, redis=redis)):
        assert not database.disposed
        assert not redis.closed
    assert database.disposed
    assert redis.closed
