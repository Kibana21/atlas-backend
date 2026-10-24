"""Health transport models (contract 0.1.0 components HealthLive, HealthReady, ReadinessChecks)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthLive(BaseModel):
    status: Literal["ok"]


class ReadinessChecks(BaseModel):
    database: Literal["ok"]
    redis: Literal["ok"]


class HealthReady(BaseModel):
    status: Literal["ok"]
    checks: ReadinessChecks
