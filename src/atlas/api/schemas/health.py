"""Health transport models (contract 0.1.0 components HealthLive, HealthReady, ReadinessChecks)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthLive(BaseModel):
    status: Literal["ok"]


class ReadinessChecks(BaseModel):
    database: Literal["ok"]
    # `redis: ok` is added with the Redis check in B-13 (CP4); until then the operation stays in
    # contract-pending.yaml and out of the generated schema.


class HealthReady(BaseModel):
    status: Literal["ok"]
    checks: ReadinessChecks
