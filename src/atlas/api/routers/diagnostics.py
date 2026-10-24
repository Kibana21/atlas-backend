"""GET /api/v1/diagnostics/build (operationId getDiagnosticsBuild; FR-27).

Always registered (the schema is environment-invariant); answers 404 NOT_FOUND unless
diagnostics are enabled, which settings refuse in uat/prod. Identity-based access arrives with
BL-104.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from atlas.api.schemas.build import BuildInfo
from atlas.api.schemas.common import ErrorEnvelope
from atlas.core.errors import AtlasError, NotFoundError
from atlas.core.settings import Settings
from atlas.persistence.db import DatabaseProbe
from atlas.version import backend_sha, read_specs_lock

router = APIRouter(tags=["diagnostics"])


@router.get(
    "/diagnostics/build",
    operation_id="getDiagnosticsBuild",
    response_model=BuildInfo,
    summary="Build provenance (environment-restricted diagnostics)",
    response_description="Build provenance of the running backend.",
    responses={404: {"model": ErrorEnvelope, "description": "Diagnostics are disabled in this environment."}},
)
async def get_diagnostics_build(request: Request) -> BuildInfo:
    settings: Settings = request.app.state.settings
    if not settings.diagnostics_enabled:
        raise NotFoundError()
    pin = read_specs_lock()
    database: DatabaseProbe = request.app.state.database
    if pin is None:
        raise AtlasError()  # a build without a valid specs.lock is misconfigured
    return BuildInfo(
        backend_sha=backend_sha(settings.build_sha),
        specs_sha=pin.specs_commit,
        contract_version=pin.contract_version,
        db_migration_revision=await database.migration_revision(),  # runtime role; None if unreadable
        environment=settings.env.value,
    )
