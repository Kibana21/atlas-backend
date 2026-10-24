"""``make worker-smoke`` (ATLAS-001 B-17): with ``make worker`` running, prove the local round trip.

Enqueues ``atlas.diagnostics.ping`` on the local Atlas queue (DB 10), waits for the marker the
worker writes, checks its value (the correlation ID) and TTL, then deletes exactly that marker.
"""

from __future__ import annotations

import asyncio
import sys

from atlas.core.correlation import new_correlation_id
from atlas.core.ids import uuid7
from atlas.core.settings import Environment, get_settings
from atlas.persistence.redis import create_redis
from atlas.workers.broker import build_broker, queue_name
from atlas.workers.producer import enqueue_ping
from atlas.workers.tasks.diagnostics import PING_TTL_SECONDS, marker_name

WAIT_SECONDS = 15.0


async def smoke() -> int:
    settings = get_settings()
    if settings.env is not Environment.LOCAL:
        print("REFUSED: worker-smoke runs against the local environment (DB 10) only", file=sys.stderr)
        return 2
    broker, tasks = build_broker(settings)
    redis = create_redis(settings, timeout=2.0)
    marker, cid = f"smoke_{uuid7().hex[:16]}", new_correlation_id()
    key = redis.key(marker_name(marker))
    await broker.startup()
    try:
        await enqueue_ping(tasks, marker, cid)
        value = None
        for _ in range(int(WAIT_SECONDS / 0.2)):
            value = await redis.get(key)
            if value is not None:
                break
            await asyncio.sleep(0.2)
        if value != cid:
            print(f"FAIL: no marker after {WAIT_SECONDS:.0f} s; is `make worker` running?", file=sys.stderr)
            print(
                f"      the message stays on {queue_name(settings.redis_key_prefix)} for the next worker",
                file=sys.stderr,
            )
            return 1
        ttl = await redis.ttl(key)
        if not 0 < ttl <= PING_TTL_SECONDS:
            print(f"FAIL: marker TTL {ttl} not in (0, {PING_TTL_SECONDS}]", file=sys.stderr)
            return 1
        print(f"OK: worker round trip on Redis DB {redis.db}; correlation ID {cid} returned; marker TTL {ttl} s")
        return 0
    finally:
        await redis.delete_exact(key)
        await redis.close()
        await broker.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(smoke()))
