"""Atlas-scoped Redis client (ATLAS-001 FR-34, B-13; DEC-045).

The local Redis server is shared with other applications. This wrapper is the only way Atlas code
talks to Redis, and it exposes only bounded, prefix-scoped operations:

* the logical DB is fixed by the settings URL (10 local, 11 test); asking for any other DB fails
  before a connection is made, and the client never issues ``SELECT``;
* every key must start with the configured ``atlas:`` prefix;
* there are no server-wide or keyspace-wide commands (no flushing, key listing, configuration or
  DB swapping); deletion is by exact key only, and scans are limited to the Atlas prefix with a hard
  result cap;
* every command has a short socket timeout, and failures surface as :class:`RedisUnavailable`
  without the URL, host or driver message.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol

from redis.asyncio import Redis

from atlas.core.logging import get_logger
from atlas.core.settings import Settings, redis_db

log = get_logger("atlas.redis")

PROBE_TIMEOUT_SECONDS = 1.0
MAX_TTL_SECONDS = 3600
SCAN_LIMIT = 1000
_KEY_PART = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class RedisIsolationError(ValueError):
    """A request would leave Atlas's Redis allocation (other DB, or a key outside the prefix)."""


class RedisUnavailable(Exception):  # noqa: N818 - domain wording ("Redis is unavailable")
    """Redis did not answer in time, or refused the command."""


def validate_allocation(url: str, prefix: str, expected_db: int) -> None:
    """Refuse a URL for another logical DB, or a prefix outside ``atlas:``. Opens no connection."""
    if redis_db(url) != expected_db:
        raise RedisIsolationError(f"Redis DB {redis_db(url)} is not the configured DB {expected_db}")
    if not prefix.startswith("atlas:") or not prefix.endswith(":"):
        raise RedisIsolationError("Redis key prefix must start with 'atlas:' and end with ':'")


class RedisProbe(Protocol):
    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class AtlasRedis:
    """Async client bound to one logical DB and one key prefix. Connects lazily, on first use."""

    def __init__(self, url: str, prefix: str, *, expected_db: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> None:
        validate_allocation(url, prefix, expected_db)
        self.db = expected_db
        self.prefix = prefix
        self._timeout = timeout
        self._client: Redis = Redis.from_url(
            url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            decode_responses=True,
            client_name="atlas",
        )

    def key(self, name: str) -> str:
        """The full Atlas key for ``name`` (``<prefix><name>``)."""
        if not _KEY_PART.fullmatch(name):
            raise RedisIsolationError("invalid key name")
        return self.prefix + name

    def _own(self, key: str) -> str:
        if not key.startswith(self.prefix) or not _KEY_PART.fullmatch(key):
            raise RedisIsolationError("key is outside the Atlas prefix")
        return key

    async def _call(self, operation: str, command: Any) -> Any:
        """Await one redis-py command (typed loosely by redis-py) under the wall-clock timeout."""
        try:
            async with asyncio.timeout(self._timeout):
                return await command
        except Exception as exc:
            log.warning("redis_command_failed", operation=operation, exc_type=type(exc).__name__)
            raise RedisUnavailable from None

    async def ping(self) -> None:
        await self._call("ping", self._client.ping())

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl must be between 1 and {MAX_TTL_SECONDS} seconds")
        await self._call("set", self._client.set(self._own(key), value, ex=ttl_seconds))

    async def get(self, key: str) -> str | None:
        value = await self._call("get", self._client.get(self._own(key)))
        return value if isinstance(value, str) else None

    async def ttl(self, key: str) -> int:
        return int(await self._call("ttl", self._client.ttl(self._own(key))))

    async def delete_exact(self, *keys: str) -> int:
        """Delete exactly these Atlas keys (no patterns)."""
        if not keys:
            return 0
        owned = [self._own(k) for k in keys]
        return int(await self._call("delete", self._client.delete(*owned)))

    async def scan_prefix(self, suffix_pattern: str = "*", *, limit: int = SCAN_LIMIT) -> list[str]:
        """Keys under the prefix matching ``<prefix><suffix_pattern>``; at most ``limit`` (bounded SCAN)."""
        if not re.fullmatch(r"[A-Za-z0-9_.:*-]{1,200}", suffix_pattern):
            raise RedisIsolationError("invalid scan pattern")
        found: list[str] = []
        cursor = 0
        while True:
            cursor, batch = await self._call(
                "scan", self._client.scan(cursor=cursor, match=self.prefix + suffix_pattern, count=100)
            )
            found.extend(k for k in batch if k.startswith(self.prefix))
            if len(found) >= limit:
                raise RedisIsolationError(f"more than {limit} keys under the prefix; refusing an unbounded scan")
            if cursor == 0:
                return sorted(found)

    async def dbsize(self) -> int:
        return int(await self._call("dbsize", self._client.dbsize()))

    async def close(self) -> None:
        await self._client.aclose()


def create_redis(settings: Settings, *, db: int | None = None, timeout: float = PROBE_TIMEOUT_SECONDS) -> AtlasRedis:
    """Client for the configured DB and prefix. ``db``, if given, must equal the configured DB."""
    configured = redis_db(settings.redis_url)
    if db is not None and db != configured:
        raise RedisIsolationError(f"Redis DB {db} requested, but this environment is configured for DB {configured}")
    return AtlasRedis(settings.redis_url, settings.redis_key_prefix, expected_db=configured, timeout=timeout)
