"""GET /api/v1/health/live (getHealthLive) and /api/v1/health/ready (getHealthReady, FR-26)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from atlas.api.schemas.health import HealthLive, HealthReady, ReadinessChecks
from atlas.core.errors import DependencyUnavailableError
from atlas.persistence.db import DatabaseProbe, DatabaseUnavailable

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


# Pending in contract-pending.yaml until B-13 adds the Redis check: the contract requires
# checks.redis, so the operation is kept out of the generated schema (and out of conformance)
# until it is complete. B-18 removes the pending entry and this exclusion together.
@router.get("/health/ready", operation_id="getHealthReady", response_model=HealthReady, include_in_schema=False)
async def get_health_ready(request: Request) -> HealthReady:
    database: DatabaseProbe = request.app.state.database
    try:
        await database.ping()
    except DatabaseUnavailable:
        raise DependencyUnavailableError(details={"failed_check": "database"}) from None
    return HealthReady(status="ok", checks=ReadinessChecks(database="ok"))
