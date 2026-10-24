"""B-09: /health/ready database check against real PostgreSQL (FR-26, AC-05 database part)."""

from __future__ import annotations

import json
import socket

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from atlas.core.settings import Settings
from atlas.main import create_app

pytestmark = pytest.mark.integration


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_ready_with_database(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_closed_port_gives_503_without_host_or_credentials(settings: Settings) -> None:
    url = make_url(settings.database_url).set(host="127.0.0.1", port=_closed_port())
    broken = settings.model_copy(update={"database_url": url.render_as_string(hide_password=False)})
    with TestClient(create_app(broken)) as client:
        r = client.get("/api/v1/health/ready")
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["details"] == {"failed_check": "database"}
    body = json.dumps(r.json())
    secret = make_url(settings.database_url).password
    leaks = ["127.0.0.1", "localhost", str(url.port), "atlas_test_app", "postgresql", *([secret] if secret else [])]
    for fragment in leaks:
        assert fragment not in body
