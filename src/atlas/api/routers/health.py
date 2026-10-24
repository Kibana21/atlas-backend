"""GET /api/v1/health/live (operationId getHealthLive)."""

from __future__ import annotations

from fastapi import APIRouter

from atlas.api.schemas.health import HealthLive

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
