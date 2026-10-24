"""Build provenance transport model (contract 0.1.0 component BuildInfo)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints


class BuildInfo(BaseModel):
    backend_sha: Annotated[str, StringConstraints(pattern=r"^([0-9a-f]{40}|unknown)$")]
    specs_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    contract_version: Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")]
    db_migration_revision: str | None
    environment: Literal["local", "dev", "test", "uat", "prod"]
