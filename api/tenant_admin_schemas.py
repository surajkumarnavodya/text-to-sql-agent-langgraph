"""Pydantic response models for `api/tenant_admin.py` (Prompt 29,
`29_TENANT_ADMIN_DASHBOARD_CONTRACT.md`) that have no identical existing
shape to reuse. `TenantOut`/`PlatformUserOut`/`RoleOut`/
`DatabaseStatusOut`/`SecurityEventOut`/`AssignRoleRequest` are imported
directly from `api.platform_admin_schemas` instead of being redefined
here -- the shape a tenant admin needs for a tenant, a user, a role, a
database's status, or a security event is identical to what the
platform-wide dashboard already returns; only the *query scope* (always
the caller's own tenant, never a parameter) differs, and that is
enforced in `api/tenant_admin.py` itself, not in these schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from api.schemas import SchemaRefreshResult

CatalogStatusLiteral = Literal["draft", "reviewed", "published", "superseded"]


class SemanticCatalogStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_count: int
    reviewed_count: int
    published_count: int
    superseded_count: int


class PendingReviewsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    onboarding_pending_by_type: dict[str, int]
    catalog_pending_count: int
    total_pending: int


class GoldenQuestionsSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID
    database_label: str
    question_count: int


class EvaluationSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID
    database_label: str
    pass_count: int
    total_count: int
    evaluated_at: datetime


class TenantSchemaRefreshResult(SchemaRefreshResult):
    """`api.schemas.SchemaRefreshResult` plus a per-database failure marker.

    Kept separate from the shared model on purpose: `POST /schema/refresh`'s
    response contract is pinned by an exact-equality test, and an extra
    always-present key there would be a silent public-API change. Only the
    tenant-admin route, which can fail one database without failing the rest,
    needs this field.
    """

    model_config = ConfigDict(frozen=True)

    error: str | None = None


class TenantSchemaRefreshResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    databases: list[TenantSchemaRefreshResult]
