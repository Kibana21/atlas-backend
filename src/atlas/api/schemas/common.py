"""Shared transport models (contract 0.1.0 components ErrorEnvelope, ErrorBody)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, StringConstraints

CorrelationId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


class ErrorBody(BaseModel):
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
    message: str
    correlation_id: CorrelationId
    details: dict[str, Any]
    recoverable: bool


class ErrorEnvelope(BaseModel):
    error: ErrorBody
