"""AtlasRedis / create_redis (FR-34, B-13, DEC-045): DB pinning, prefix scoping, bounded operations."""

from __future__ import annotations

import asyncio
import fnmatch
from typing import Any

import pytest
import redis.asyncio

import atlas.persistence.redis as r
from tests.conftest import make_settings

pytestmark = [pytest.mark.unit, pytest.mark.security]
PREFIX = "atlas:test:unit:"


class _FakeClient:
    """Records commands; holds data in a dict. Mirrors the redis-py async methods AtlasRedis uses."""

    def __init__(self, data: dict[str, str] | None = None, *, fail: Exception | None = None, delay: float = 0) -> None:
        self.data = dict(data or {})
        self.fail, self.delay = fail, delay
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.ttls: dict[str, int] = {}
        self.closed = False

    async def _run(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail

    async def ping(self) -> bool:
        await self._run("ping")
        return True

    async def set(self, key: str, value: str, ex: int) -> bool:
        await self._run("set", key, value, ex)
        self.data[key], self.ttls[key] = value, ex
        return True

    async def get(self, key: str) -> str | None:
        await self._run("get", key)
        return self.data.get(key)

    async def ttl(self, key: str) -> int:
        await self._run("ttl", key)
        return self.ttls.get(key, -2)

    async def delete(self, *keys: str) -> int:
        await self._run("delete", *keys)
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def scan(self, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        await self._run("scan", cursor, match, count)
        return 0, [k for k in self.data if fnmatch.fnmatchcase(k, match)]

    async def dbsize(self) -> int:
        await self._run("dbsize")
        return len(self.data)

    async def aclose(self) -> None:
        self.closed = True


def _client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient, timeout: float = 1.0) -> r.AtlasRedis:
    seen: dict[str, Any] = {}

    def from_url(url: str, **kwargs: Any) -> _FakeClient:
        seen.update(url=url, **kwargs)
        return fake

    monkeypatch.setattr(redis.asyncio.Redis, "from_url", from_url)
    client = r.create_redis(make_settings(), timeout=timeout)
    assert seen["url"].endswith("/11")
    assert seen["socket_timeout"] == seen["socket_connect_timeout"] == timeout
    return client


def test_factory_refuses_any_other_db_before_connecting() -> None:
    with pytest.raises(r.RedisIsolationError, match="DB 1 requested"):
        r.create_redis(make_settings(), db=1)
    with pytest.raises(r.RedisIsolationError):
        r.AtlasRedis("redis://localhost:6379/1", PREFIX, expected_db=11)
    with pytest.raises(r.RedisIsolationError, match="prefix"):
        r.AtlasRedis("redis://localhost:6379/11", "other:", expected_db=11)


def test_keys_are_prefixed_and_foreign_keys_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _FakeClient())
    assert client.key("ping:abc") == PREFIX + "ping:abc"
    for bad in ("", "a b", "x" * 201, "*"):
        with pytest.raises(r.RedisIsolationError):
            client.key(bad)
    for foreign in ("other:key", "atlas:local:key", "atlas:test:other:key"):
        with pytest.raises(r.RedisIsolationError):
            asyncio.run(client.get(foreign))
        with pytest.raises(r.RedisIsolationError):
            asyncio.run(client.delete_exact(foreign))


@pytest.mark.anyio
async def test_set_get_ttl_delete_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    client = _client(monkeypatch, fake)
    key = client.key("ping:m1")
    await client.set_with_ttl(key, "v", 60)
    assert await client.get(key) == "v"
    assert await client.ttl(key) == 60
    assert await client.delete_exact() == 0
    assert await client.delete_exact(key) == 1
    assert await client.get(key) is None
    for ttl in (0, -1, r.MAX_TTL_SECONDS + 1):
        with pytest.raises(ValueError, match="ttl"):
            await client.set_with_ttl(key, "v", ttl)
    await client.ping()
    assert await client.dbsize() == 0
    await client.close()
    assert fake.closed
    assert {name for name, _ in fake.calls} <= {"ping", "set", "get", "ttl", "delete", "scan", "dbsize"}


@pytest.mark.anyio
async def test_scan_is_prefix_scoped_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({PREFIX + "a": "1", PREFIX + "b": "2", "other:a": "x", "atlas:local:a": "y"})
    client = _client(monkeypatch, fake)
    assert await client.scan_prefix() == [PREFIX + "a", PREFIX + "b"]
    assert fake.calls[-1] == ("scan", (0, PREFIX + "*", 100))
    with pytest.raises(r.RedisIsolationError, match="more than 1"):
        await client.scan_prefix(limit=1)
    with pytest.raises(r.RedisIsolationError):
        await client.scan_prefix("a b")


@pytest.mark.anyio
async def test_failures_and_timeouts_hide_connection_details(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _FakeClient(fail=OSError("redis://localhost:6379/11 refused")))
    with pytest.raises(r.RedisUnavailable) as caught:
        await client.ping()
    assert str(caught.value) == ""
    assert caught.value.__cause__ is None
    slow = _client(monkeypatch, _FakeClient(delay=1), timeout=0.05)
    with pytest.raises(r.RedisUnavailable):
        await slow.ping()


def test_stale_connections_are_replaced_once_on_connection_errors_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A001-DEF-01: one reconnect for a connection that died while idle; timeouts are not retried."""
    captured: dict[str, Any] = {}

    def from_url(url: str, **kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(redis.asyncio.Redis, "from_url", from_url)
    r.AtlasRedis("redis://localhost:6379/11", PREFIX, expected_db=11)
    retry = captured["retry"]
    assert retry._retries == r.RECONNECT_ATTEMPTS == 1
    assert retry._supported_errors == (redis.exceptions.ConnectionError,)
    assert "retry_on_timeout" not in captured
    assert "retry_on_error" not in captured
