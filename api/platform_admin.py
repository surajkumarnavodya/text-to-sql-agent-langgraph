"""The global platform-admin dashboard -- Prompt 28
(`28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`).

**Every route here is gated by `api.identity_authz.require_platform_admin`
-- a genuinely different dimension from every other admin check in this
codebase.** Every other `admin`-gated route (`/schema/refresh`,
`/metrics/performance`, `api/onboarding.py`'s `ONBOARDING_MANAGE`,
`api/semantic_catalog.py`'s `CATALOG_MANAGE`, ...) scopes to the actor's
*own* tenant via ABAC, by design -- a tenant's own admin must never see or
act on another tenant's data. This module is the deliberate exception:
its entire purpose is cross-tenant visibility and administration, which is
why it requires the separate `identity.rbac.Permission.PLATFORM_ADMIN`
(granted only to the `platform_admin` role) rather than the ordinary
tenant-scoped `admin` role -- see that permission's own docstring for the
full "platform-admin versus tenant-admin" rationale this prompt's own
testing requirement names explicitly.

**Reuses existing functionality end to end, builds almost nothing new
from scratch**: `identity/repositories/tenants.py`'s full CRUD existed
since Prompt 20 and was never exposed by any route until now;
`identity.repositories.users.assign_role`/`remove_role` are the exact
same functions `scripts/bootstrap_admin.py`-style tooling already used;
`observability.metrics.get_default_metrics().snapshot(tenant_id=None)`
already supported the merged, cross-tenant view, "deliberately not
reachable through the HTTP route" per that module's own docstring --
until this one. `GET /health` and `GET /models` are deliberately **not**
duplicated here: both are already unscoped/public and safe for the
platform-admin dashboard's frontend to call directly.

**Never exposes a credential.** `DatabaseStatusOut` has no
`db_user`/`db_password`/`db_connection_string` field at all, and
`GET /platform-admin/config-status` only ever reports `Settings` fields
that are already plain `bool` -- a `SecretStr`-typed field could never
satisfy that filter, so this is safe by construction, not by a redaction
step that could be forgotten.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from identity.models import User
from identity.repositories.audit import list_audit_events, record_audit_event
from identity.repositories.onboarding import (
    count_pending_review_items_across_tenants,
    list_jobs_across_tenants,
)
from identity.repositories.semantic_catalog import list_entries_across_tenants
from identity.repositories.tenants import create_tenant, list_tenants, set_tenant_status
from identity.repositories.users import assign_role as _assign_role
from identity.repositories.users import (
    count_users_by_tenant,
    get_user_by_id,
    get_user_roles,
    list_roles_with_permissions,
    list_users,
)
from identity.repositories.users import remove_role as _remove_role
from sqlalchemy.orm import Session

from api.identity_authz import require_local_user, require_platform_admin
from api.platform_admin_schemas import (
    AssignRoleRequest,
    AuditLogOut,
    ConfigStatusOut,
    CreateTenantRequest,
    DatabaseStatusOut,
    PlatformOnboardingJobOut,
    PlatformUserOut,
    RoleOut,
    SecurityEventOut,
    SemanticReviewQueueOut,
    SetTenantStatusRequest,
    TenantOut,
)
from api.schemas import PerformanceMetricsResponse, RequestMetricOut, StageMetricOut
from config.settings import get_settings
from db.connection import test_connection
from observability.metrics import get_default_metrics
from security.audit_log import log_security_event, recent_security_events
from security.redaction import redact_secrets

router = APIRouter(prefix="/platform-admin", tags=["platform-admin"])


def _require_platform_admin_pair(
    user_and_session: tuple[User, Session] = Depends(require_local_user),
    _permission_check: tuple[User, Session] = Depends(require_platform_admin),
) -> tuple[User, Session]:
    """Composes `require_local_user` (loads the caller) with
    `require_platform_admin` (checks the permission) into the one
    dependency every route below actually uses -- both already resolve
    to the same `(User, Session)` shape, so this just avoids every route
    declaring two near-identical `Depends(...)` parameters."""
    return user_and_session


@router.get("/tenants", response_model=list[TenantOut])
def list_platform_tenants(
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[TenantOut]:
    _, session = user_and_session
    tenants = list_tenants(session)
    user_counts = count_users_by_tenant(session)
    return [
        TenantOut(
            id=tenant.id,
            name=tenant.name,
            status=tenant.status,  # type: ignore[arg-type]
            user_count=user_counts.get(tenant.id, 0),
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
        )
        for tenant in tenants
    ]


@router.post("/tenants", response_model=TenantOut)
def create_platform_tenant(
    payload: CreateTenantRequest,
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> TenantOut:
    user, session = user_and_session
    try:
        tenant = create_tenant(session, tenant_id=payload.tenant_id, name=payload.name)
    except Exception as exc:  # noqa: BLE001 - a duplicate tenant id is the realistic failure
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit_event(
        session,
        actor_user_id=user.id,
        action="tenant_created",
        resource_type="tenant",
        resource_id=tenant.id,
        outcome="success",
        metadata={"name": tenant.name},
    )
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        status=tenant.status,  # type: ignore[arg-type]
        user_count=0,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


@router.post("/tenants/{tenant_id}/status", response_model=TenantOut)
def set_platform_tenant_status(
    tenant_id: str,
    payload: SetTenantStatusRequest,
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> TenantOut:
    """Suspends or re-activates a tenant. Takes effect on the very next
    request for every identity-backed route (`require_local_user` re-checks
    the tenant's status live), and blocks refresh immediately. The one
    exception is `/ask`'s token-only path, bounded by access-token lifetime
    -- see `security.tenancy.resolve_tenant_context`'s own docstring."""
    user, session = user_and_session
    tenant = set_tenant_status(session, tenant_id=tenant_id, status=payload.status)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    record_audit_event(
        session,
        actor_user_id=user.id,
        action="tenant_status_changed",
        resource_type="tenant",
        resource_id=tenant.id,
        outcome="success",
        metadata={"status": payload.status},
    )
    log_security_event(
        "platform_admin_tenant_status_changed",
        "warning" if payload.status == "suspended" else "info",
        f"Platform admin set tenant {tenant.id!r} status to {payload.status!r}.",
        tenant_id=tenant.id,
        actor_user_id=str(user.id),
    )
    user_counts = count_users_by_tenant(session)
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        status=tenant.status,  # type: ignore[arg-type]
        user_count=user_counts.get(tenant.id, 0),
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


@router.get("/users", response_model=list[PlatformUserOut])
def list_platform_users(
    tenant_id: str | None = Query(default=None),
    role_name: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[PlatformUserOut]:
    _, session = user_and_session
    accounts = list_users(session, tenant_id=tenant_id, role_name=role_name, status=status_filter)
    return [
        PlatformUserOut(
            id=account.id,
            email=account.email,
            display_name=account.display_name,
            tenant_id=account.tenant_id,
            status=account.status,
            roles=list(get_user_roles(session, account.id)),
            created_at=account.created_at,
            last_login_at=account.last_login_at,
        )
        for account in accounts
    ]


@router.post("/users/{user_id}/roles", response_model=PlatformUserOut)
def assign_platform_user_role(
    user_id: uuid.UUID,
    payload: AssignRoleRequest,
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> PlatformUserOut:
    user, session = user_and_session
    target = get_user_by_id(session, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    try:
        _assign_role(
            session, user_id=target.id, role_name=payload.role_name, assigned_by_user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    record_audit_event(
        session,
        actor_user_id=user.id,
        subject_user_id=target.id,
        action="role_assigned",
        resource_type="user",
        resource_id=str(target.id),
        outcome="success",
        metadata={"role_name": payload.role_name},
    )
    return PlatformUserOut(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        tenant_id=target.tenant_id,
        status=target.status,
        roles=list(get_user_roles(session, target.id)),
        created_at=target.created_at,
        last_login_at=target.last_login_at,
    )


@router.delete("/users/{user_id}/roles/{role_name}", response_model=PlatformUserOut)
def remove_platform_user_role(
    user_id: uuid.UUID,
    role_name: str,
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> PlatformUserOut:
    user, session = user_and_session
    target = get_user_by_id(session, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    # A platform admin revoking their own cross-tenant access in one click is
    # the easiest way to lock the whole operator dashboard out with no one
    # left to undo it from the UI. Another platform admin can still do it.
    if target.id == user.id and role_name == "platform_admin":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot remove your own platform_admin role. Ask another platform admin.",
        )
    _remove_role(session, user_id=target.id, role_name=role_name)
    record_audit_event(
        session,
        actor_user_id=user.id,
        subject_user_id=target.id,
        action="role_removed",
        resource_type="user",
        resource_id=str(target.id),
        outcome="success",
        metadata={"role_name": role_name},
    )
    return PlatformUserOut(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        tenant_id=target.tenant_id,
        status=target.status,
        roles=list(get_user_roles(session, target.id)),
        created_at=target.created_at,
        last_login_at=target.last_login_at,
    )


@router.get("/roles", response_model=list[RoleOut])
def list_platform_roles(
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[RoleOut]:
    _, session = user_and_session
    return [
        RoleOut(
            name=entry.role.name,
            description=entry.role.description,
            is_system_role=entry.role.is_system_role,
            permissions=list(entry.permissions),
        )
        for entry in list_roles_with_permissions(session)
    ]


@router.get("/databases", response_model=list[DatabaseStatusOut])
def list_platform_databases(
    _user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[DatabaseStatusOut]:
    """Every configured connection (`Settings.databases`) plus a real,
    non-cached reachability check -- the same `db.connection
    .test_connection` round trip `GET /health` already performs per
    database, re-exposed here alongside the *configuration* (host, port,
    which tenants it's bound to) that unauthenticated `/health` cannot
    show. Never includes `db_user`/`db_password`/`db_connection_string`."""
    settings = get_settings()
    results = []
    for config in settings.databases:
        outcome = test_connection(config)
        detail = outcome.message if outcome.success else redact_secrets(outcome.message, config)
        results.append(
            DatabaseStatusOut(
                name=config.name,
                db_type=config.db_type,
                db_host=config.db_host,
                db_port=config.db_port,
                db_name=config.db_name,
                db_schema=config.db_schema,
                tenant_ids=list(config.tenant_ids),
                healthy=outcome.success,
                detail=detail,
            )
        )
    return results


@router.get("/semantic-review-queue", response_model=SemanticReviewQueueOut)
def platform_semantic_review_queue(
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> SemanticReviewQueueOut:
    """Cross-tenant counts of outstanding SME review work -- the two real
    review systems this platform has (Prompt 26's onboarding review items,
    Prompt 27's governed semantic catalog), which neither feeds the
    other (see `frontend/src/pages/SemanticReview.tsx`'s own docstring).
    This is a count rollup, not a work queue UI of its own -- an operator
    drills into a specific tenant's own job/catalog review surface
    (`/db-onboarding`, `/semantic-review`) to actually act on an item."""
    _, session = user_and_session
    draft = len(list_entries_across_tenants(session, status="draft"))
    reviewed = len(list_entries_across_tenants(session, status="reviewed"))
    published = len(list_entries_across_tenants(session, status="published"))
    onboarding_pending = count_pending_review_items_across_tenants(session)
    return SemanticReviewQueueOut(
        catalog_draft_count=draft,
        catalog_reviewed_count=reviewed,
        catalog_published_count=published,
        onboarding_pending_by_type=onboarding_pending,
        total_pending=draft + reviewed + sum(onboarding_pending.values()),
    )


@router.get("/jobs", response_model=list[PlatformOnboardingJobOut])
def list_platform_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[PlatformOnboardingJobOut]:
    _, session = user_and_session
    jobs = list_jobs_across_tenants(session, status=status_filter)
    return [
        PlatformOnboardingJobOut(
            id=job.id,
            tenant_id=job.tenant_id,
            database_label=job.database_label,
            status=job.status,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        for job in jobs
    ]


@router.get("/metrics", response_model=PerformanceMetricsResponse)
def platform_metrics(
    _user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> PerformanceMetricsResponse:
    """The merged, process-wide rollup `observability.metrics
    .PerformanceMetrics.snapshot()` has supported since Prompt 20 but
    never had an HTTP route reach -- `GET /metrics/performance` reads only
    the *caller's own* tenant slice, by design (see that route's own
    docstring); this route is the deliberately cross-tenant counterpart,
    gated on `PLATFORM_ADMIN` rather than the tenant-scoped admin check."""
    snapshot = get_default_metrics().snapshot(tenant_id=None)
    return PerformanceMetricsResponse(
        started_at=snapshot["started_at"],
        window_requests=snapshot["window_requests"],
        max_window_requests=snapshot["max_window_requests"],
        requests=RequestMetricOut(**snapshot["requests"]),
        stages=[StageMetricOut(**stage) for stage in snapshot["stages"]],
        status_counts=snapshot["status_counts"],
        tenant_id=None,
        result_cache_hits=snapshot["result_cache_hits"],
        result_cache_misses=snapshot["result_cache_misses"],
        database_concurrency_rejections=snapshot["database_concurrency_rejections"],
    )


@router.get("/security-events", response_model=list[SecurityEventOut])
def platform_security_events(
    limit: int = Query(default=100, ge=1, le=1000),
    severity: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    _user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[SecurityEventOut]:
    """The in-process ring buffer `security.audit_log` now feeds from
    inside `log_security_event` itself -- every one of this codebase's
    ~40 existing call sites, with zero changes to any of them. See that
    module's own docstring for the disclosed single-process/in-memory/
    resets-on-restart limits."""
    events = recent_security_events(
        limit=limit, severity=severity, event_type=event_type  # type: ignore[arg-type]
    )
    return [SecurityEventOut(**event) for event in events]


@router.get("/audit-logs", response_model=list[AuditLogOut])
def platform_audit_logs(
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> list[AuditLogOut]:
    """Reads back `identity.models.AuditLog` -- a real table that existed
    since this project's identity schema was first built, with
    `Permission.AUDIT_READ_ANY` seeded since then too, but which nothing
    ever wrote to or read from until this prompt (see
    `identity/repositories/audit.py`'s own docstring). Only this prompt's
    own new mutating actions (tenant status changes, role assignment)
    populate it -- a disclosed, narrower scope than "every mutation in
    this application," named as a known limitation rather than silently
    implied to be complete."""
    _, session = user_and_session
    events = list_audit_events(
        session, action=action, resource_type=resource_type, outcome=outcome, limit=limit
    )
    return [
        AuditLogOut(
            id=event.id,
            actor_user_id=event.actor_user_id,
            subject_user_id=event.subject_user_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome,  # type: ignore[arg-type]
            created_at=event.created_at,
            metadata=event.metadata_json,
        )
        for event in events
    ]


@router.get("/config-status", response_model=ConfigStatusOut)
def platform_config_status(
    _user_and_session: tuple[User, Session] = Depends(_require_platform_admin_pair),
) -> ConfigStatusOut:
    """Every boolean feature flag on `Settings` (`enable_*`/`*_enabled`),
    and nothing else -- a `pydantic.SecretStr`-typed field can never
    satisfy `isinstance(value, bool)`, so this is safe by construction,
    not by a redaction step that could be forgotten or bypassed."""
    settings = get_settings()
    flags = {
        field_name: value
        for field_name, value in settings.model_dump().items()
        if isinstance(value, bool)
    }
    return ConfigStatusOut(flags=flags)
