"""Health transport models (contract 0.1.0 component HealthLive)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthLive(BaseModel):
    status: Literal["ok"]
