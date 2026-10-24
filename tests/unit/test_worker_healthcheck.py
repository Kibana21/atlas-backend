"""Worker container health check (B-21): exit 0 iff the worker's own Redis DB answers PING."""

from __future__ import annotations

from typing import Any

import pytest

from atlas.persistence.redis import RedisUnavailable
from atlas.workers import healthcheck
from tests.conftest import make_settings

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, ok: bool) -> None:
        self.ok, self.closed = ok, False

    async def ping(self) -> None:
        if not self.ok:
            raise RedisUnavailable

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
def test_exit_code_follows_redis_ping(monkeypatch: pytest.MonkeyPatch, ok: bool, code: int) -> None:
    client = _Client(ok)
    seen: dict[str, Any] = {}

    def factory(settings: Any, *, timeout: float) -> _Client:
        seen.update(db=settings.redis_url.rsplit("/", 1)[-1], timeout=timeout)
        return client

    monkeypatch.setattr(healthcheck, "get_settings", lambda: make_settings())
    monkeypatch.setattr(healthcheck, "create_redis", factory)
    assert healthcheck.main() == code
    assert client.closed
    assert seen == {"db": "11", "timeout": 2.0}  # the configured DB only, bounded
