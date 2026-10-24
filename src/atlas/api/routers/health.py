"""GET /api/v1/health/live (getHealthLive) and /api/v1/health/ready (getHealthReady, FR-26)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from atlas.api.schemas.common import ErrorEnvelope
from atlas.api.schemas.health import HealthLive, HealthReady, ReadinessChecks
from atlas.core.errors import DependencyUnavailableError
from atlas.persistence.db import DatabaseProbe, DatabaseUnavailable
from atlas.persistence.redis import RedisProbe, RedisUnavailable

router = APIRouter(tags=["health"])


@router.get(
    "/health/live",
    operation_id="getHealthLive",
    response_model=HealthLive,
    summary="Process liveness",
    response_description="The API process is running.",
)
def get_health_live() -> HealthLive:
    return HealthLive(status="ok")


@router.get(
    "/health/ready",
    operation_id="getHealthReady",
    response_model=HealthReady,
    summary="Dependency readiness (PostgreSQL and Redis)",
    response_description="All required dependencies responded within their timeout.",
    responses={
        503: {
            "model": ErrorEnvelope,
            "description": "A required dependency is unavailable (code DEPENDENCY_UNAVAILABLE; "
            "details.failed_check names it, without host or credential information).",
        }
    },
)
async def get_health_ready(request: Request) -> HealthReady:
    """Both checks run concurrently, each bounded to 1 s; the first failed check (in order) is named."""
    database: DatabaseProbe = request.app.state.database
    redis: RedisProbe = request.app.state.redis
    outcomes = await asyncio.gather(database.ping(), redis.ping(), return_exceptions=True)
    for name, outcome in zip(("database", "redis"), outcomes, strict=True):
        if isinstance(outcome, (DatabaseUnavailable, RedisUnavailable)):
            raise DependencyUnavailableError(details={"failed_check": name})
        if isinstance(outcome, BaseException):
            raise outcome
    return HealthReady(status="ok", checks=ReadinessChecks(database="ok", redis="ok"))
