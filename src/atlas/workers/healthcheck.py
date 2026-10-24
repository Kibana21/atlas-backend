"""Worker container health check (``python -m atlas.workers.healthcheck``; ATLAS-001 B-21).

Healthy (exit 0) when the worker's configured Redis DB answers ``PING`` within 2 s. Read-only, and
it uses the same Atlas-scoped client as the worker, so it can never reach another DB.
"""

from __future__ import annotations

import asyncio
import sys

from atlas.core.settings import get_settings
from atlas.persistence.redis import RedisUnavailable, create_redis


async def check() -> int:
    client = create_redis(get_settings(), timeout=2.0)
    try:
        await client.ping()
    except RedisUnavailable:
        print("unhealthy: redis unavailable", file=sys.stderr)
        return 1
    finally:
        await client.close()
    return 0


def main() -> int:
    return asyncio.run(check())


if __name__ == "__main__":
    sys.exit(main())
