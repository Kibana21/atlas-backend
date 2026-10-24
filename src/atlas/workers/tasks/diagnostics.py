"""``atlas.diagnostics.ping`` (ATLAS-001 FR-32, B-14): proves the API-to-worker seam.

Writes ``<prefix>ping:<marker_id>`` = correlation ID with a TTL of at most 60 s and logs the
correlation ID. Idempotent: a redelivered message rewrites the same key with the same value, so
at-least-once delivery is safe. No automatic retries in ATLAS-001 (DEC-046 → BL-201).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from taskiq import Context, TaskiqDepends

from atlas.core.logging import get_logger
from atlas.persistence.redis import AtlasRedis

log = get_logger("atlas.workers.diagnostics")

PING_TASK = "atlas.diagnostics.ping"
PING_TTL_SECONDS = 60
PING_TIMEOUT_SECONDS = 10
REDIS_STATE_ATTRIBUTE = "atlas_redis"
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


class PingPayload(BaseModel):
    """Validated arguments of the ping task (checked by the producer and again by the worker)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    marker_id: Identifier
    correlation_id: Identifier


class InvalidTaskPayload(ValueError):  # noqa: N818 - reads as "invalid task payload"
    """Task arguments failed validation. Names the fields only: argument values are never logged."""

    def __init__(self, fields: list[str]) -> None:
        super().__init__(f"invalid task payload fields: {', '.join(fields)}")
        self.fields = fields


def validate_ping(marker_id: str, correlation_id: str) -> PingPayload:
    try:
        return PingPayload(marker_id=marker_id, correlation_id=correlation_id)
    except ValidationError as exc:
        raise InvalidTaskPayload(sorted({".".join(map(str, e["loc"])) for e in exc.errors()})) from None


def marker_name(marker_id: str) -> str:
    return f"ping:{marker_id}"


async def ping(marker_id: str, correlation_id: str, context: Annotated[Context, TaskiqDepends()]) -> None:
    payload = validate_ping(marker_id, correlation_id)
    redis: AtlasRedis = getattr(context.state, REDIS_STATE_ATTRIBUTE)
    key = redis.key(marker_name(payload.marker_id))
    await redis.set_with_ttl(key, payload.correlation_id, PING_TTL_SECONDS)
    log.info("diagnostics_ping", marker_id=payload.marker_id, task_correlation_id=payload.correlation_id)
