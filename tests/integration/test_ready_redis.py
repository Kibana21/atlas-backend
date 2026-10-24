"""B-13: /health/ready with real PostgreSQL and Redis; Redis factory isolation (FR-26, FR-34, AC-05, AC-11)."""

from __future__ import annotations

import json
import socket

import pytest
from fastapi.testclient import TestClient

from atlas.core.settings import Settings
from atlas.main import create_app
from atlas.persistence.redis import RedisIsolationError, create_redis

pytestmark = pytest.mark.integration


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_ready_with_database_and_redis(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_closed_redis_port_gives_503_naming_redis_without_connection_details(settings: Settings) -> None:
    port = _closed_port()
    broken = settings.model_copy(update={"redis_url": f"redis://127.0.0.1:{port}/11"})
    with TestClient(create_app(broken)) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.json()["error"]["details"] == {"failed_check": "redis"}
    body = json.dumps(r.json())
    for fragment in ("127.0.0.1", str(port), "redis://", "6379"):
        assert fragment not in body


def test_factory_rejects_db_1_before_connecting(settings: Settings) -> None:
    with pytest.raises(RedisIsolationError, match="DB 1"):
        create_redis(settings, db=1)


@pytest.mark.anyio
async def test_client_is_bound_to_db_11_and_the_run_prefix(settings: Settings, key_tracker: list[str]) -> None:
    client = create_redis(settings)
    try:
        key = client.key("isolation:probe")
        key_tracker.append(key)
        assert key.startswith(settings.redis_key_prefix)
        await client.set_with_ttl(key, "1", 30)
        assert await client.scan_prefix("isolation:*") == [key]
        info = await client._client.client_info()  # read-only: the connection's own DB
        assert int(info["db"]) == 11
    finally:
        await client.close()
