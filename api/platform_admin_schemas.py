"""Pydantic request/response models for `api/platform_admin.py` (Prompt
28, `28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`). Mirrors
`api/onboarding_schemas.py`'s own conventions: request models get
`str_strip_whitespace=True` + `extra="forbid"`, response models get
`frozen=True`.

**No response model here, or anywhere else in this file's own router,
ever includes a password, connection string, or other secret** --
`DatabaseStatusOut` deliberately omits `db_user`/`db_password`/
`db_connection_string` entirely rather than redacting them, the same
"never even put it in the shape" posture `OnboardingJobOut`'s own
docstring already established for this codebase's other admin surfaces.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TenantStatusLiteral = Literal["active", "suspended"]
AuditOutcomeLiteral = Literal["success", "failure", "denied"]
SecuritySeverityLiteral = Literal["info", "warning", "critical"]


class CreateTenantRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)


class SetTenantStatusRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: TenantStatusLiteral


class AssignRoleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    role_name: str = Field(..., min_length=1, max_length=64)


class TenantOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    status: TenantStatusLiteral
    user_count: int
    created_at: datetime
    updated_at: datetime


class PlatformUserOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    tenant_id: str
    status: str
    roles: list[str]
    created_at: datetime
    last_login_at: datetime | None


class RoleOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None
    is_system_role: bool
    permissions: list[str]


class DatabaseStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    db_type: str
    db_host: str | None
    db_port: int | None
    db_name: str | None
    db_schema: str | None
    tenant_ids: list[str]
    healthy: bool
    detail: str


class SemanticReviewQueueOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    catalog_draft_count: int
    catalog_reviewed_count: int
    catalog_published_count: int
    onboarding_pending_by_type: dict[str, int]
    total_pending: int


class PlatformOnboardingJobOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: str
    database_label: str
    status: str
    created_at: datetime
    updated_at: datetime


class SecurityEventOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: str
    event_type: str
    severity: SecuritySeverityLiteral
    detail: str
    correlation_id: str | None
    tenant_id: str | None
    context: dict[str, str]


class AuditLogOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    subject_user_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcomeLiteral
    created_at: datetime
    metadata: dict[str, Any] | None


class ConfigStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    flags: dict[str, bool]
