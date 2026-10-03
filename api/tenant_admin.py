"""The tenant/client admin dashboard -- Prompt 29
(`29_TENANT_ADMIN_DASHBOARD_CONTRACT.md`), the tenant-scoped counterpart
to Prompt 28's platform-wide dashboard (`api/platform_admin.py`).

**No new RBAC permission or role exists here, unlike Prompt 28.** A
tenant admin is exactly what the existing, tenant-scoped `admin` role
(`identity.rbac`'s `_ADMIN` permission set) already means -- this module
adds no new capability, only a consolidated *view* over capabilities
that mostly already existed, each independently re-scoped to the
caller's own tenant.

**The one load-bearing rule every route in this file follows, with no
exception**: `tenant_id` is read *exactly once*, from
`security.tenancy.resolve_actor_tenant_id(user)`, and never from a
request body, query parameter, or path parameter. There is no
`tenant_id` argument on any route signature here at all -- unlike
`api/platform_admin.py`, which takes one deliberately (its entire
purpose is cross-tenant access, gated on the separate `PLATFORM_ADMIN`
permission). This is the structural guarantee behind "tenant admins
cannot access platform-wide or other-tenant data" -- not a check that
could be forgotten on one route, but a shape no route here can violate.

**Heavy reuse, almost nothing new**: `identity.repositories.tenants
.get_tenant`, `identity.repositories.users.list_users`/
`list_roles_with_permissions`/`assign_role`/`remove_role`,
`identity.repositories.semantic_catalog.list_entries`,
`identity.repositories.onboarding.list_jobs_for_tenant`/
`list_review_items`/`list_artifacts`, and `Settings.databases_for_tenant`
all already existed from Prompt 20/26/27/28 -- every route below is a
thin, tenant-scoped aggregation over them. `GET /metrics/performance`
and `GET /recommendations`(`/metrics`) are **not duplicated here** --
both already resolve and scope to the caller's own tenant (confirmed by
reading them), so the frontend calls those existing routes directly for
AI-usage/analytics/recommendations. Managing an onboarding job's own
lifecycle (discover/publish/cancel/retry) is **not duplicated here**
either -- the existing `/onboarding/*` routes already do this, already
tenant-ABAC'd; this dashboard's own Jobs section links to the existing
`/db-onboarding` page rather than re-implementing its review workflow,
the same precedent `SemanticReview.tsx` already set for the identical
reason.

**One real, newly-relevant tenant-isolation gap this prompt closes**:
the existing `POST /schema/refresh` (`api/main.py`) refreshes *every*
configured database with no tenant filter at all -- correct for its
existing platform-operator use (gated on `agent.authz
.Permission.SCHEMA_REFRESH`, a base-role check with no tenant scoping
concept at all), but wrong to hand a tenant-admin dashboard as-is: a
tenant admin triggering it would re-introspect and re-embed *other*
tenants' databases too. Rather than change that existing route (master-
contract rule 4: preserve working behavior), this module adds a new,
genuinely tenant-scoped refresh action that loops only
`Settings.databases_for_tenant(caller_tenant_id)`, reusing the exact same
`embeddings.schema_indexer.refresh_schema_index` function per database.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from identity.models import User
from identity.rbac import Permission as IdentityPermission
from identity.repositories.onboarding import list_artifacts, list_jobs_for_tenant
from identity.repositories.onboarding import list_review_items as _list_review_items
from identity.repositories.semantic_catalog import list_entries
from identity.repositories.tenants import get_tenant
from identity.repositories.users import assign_role as _assign_role
from identity.repositories.users import (
    get_user_by_id,
    get_user_roles,
    list_roles_with_permissions,
    list_users,
)
from identity.repositories.users import remove_role as _remove_role
from sqlalchemy.orm import Session

from api.identity_authz import require_identity_permission, require_local_user
from api.platform_admin_schemas import (
    AssignRoleRequest,
    DatabaseStatusOut,
    PlatformUserOut,
    RoleOut,
    SecurityEventOut,
    TenantOut,
)
from api.schemas import SchemaRefreshResponse, SchemaRefreshResult
from api.tenant_admin_schemas import (
    EvaluationSummaryOut,
    GoldenQuestionsSummaryOut,
    PendingReviewsOut,
    SemanticCatalogStatusOut,
)
from config.settings import get_settings
from db.connection import get_read_only_engine, test_connection
from embeddings.schema_indexer import get_last_discovery_diff, refresh_schema_index
from security.audit_log import log_security_event, recent_security_events
from security.redaction import redact_secrets
from security.tenancy import resolve_actor_tenant_id

router = APIRouter(prefix="/tenant-admin", tags=["tenant-admin"])

#: A tenant admin may never assign/remove this role -- it crosses tenant
#: boundaries, exactly what this whole dashboard structurally cannot do
#: (see this module's own docstring). Checked in `assign_tenant_user_role`
#: even though a tenant admin's own permission set (`USERS_ASSIGN_ROLES`)
#: has no innate concept of this restriction -- the restriction is this
#: route's own, not a reuse of `semantic.catalog_policy`-style ABAC.
_RESTRICTED_ROLE_NAMES = frozenset({"platform_admin"})


def _require_tenant(
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> tuple[User, Session, str]:
    """Resolves and returns the caller's own tenant id alongside the usual
    `(User, Session)` pair -- the one place every route below gets its
    `tenant_id` from. See this module's own docstring for why no route
    signature in this file ever accepts one as a parameter instead."""
    user, session = user_and_session
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not resolve a tenant for this account.",
        )
    return user, session, tenant_id


def _require_dashboard_read(
    triple: tuple[User, Session, str] = Depends(_require_tenant),
    _permission_check: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.ADMIN_DASHBOARD_READ)
    ),
) -> tuple[User, Session, str]:
    return triple


@router.get("/profile", response_model=TenantOut)
def get_tenant_profile(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> TenantOut:
    _, session, tenant_id = triple
    tenant = get_tenant(session, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Your account's own tenant could not be found.",
        )
    user_count = len(list_users(session, tenant_id=tenant_id, limit=10_000))
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        status=tenant.status,  # type: ignore[arg-type]
        user_count=user_count,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


@router.get("/databases", response_model=list[DatabaseStatusOut])
def list_tenant_databases(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> list[DatabaseStatusOut]:
    """Only the connections this tenant may actually query
    (`Settings.databases_for_tenant`) -- never every configured
    connection, which is exactly what distinguishes this from
    `api.platform_admin.list_platform_databases`."""
    _, _, tenant_id = triple
    settings = get_settings()
    results = []
    for config in settings.databases_for_tenant(tenant_id):
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


@router.post("/databases/refresh", response_model=SchemaRefreshResponse)
def refresh_tenant_databases(
    triple: tuple[User, Session, str] = Depends(_require_tenant),
    _permission_check: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.ONBOARDING_MANAGE)
    ),
) -> SchemaRefreshResponse:
    """Re-introspects and re-embeds only *this tenant's own* configured
    databases -- see this module's own docstring for why the existing,
    unscoped `POST /schema/refresh` is never reused for this action.
    Gated on `ONBOARDING_MANAGE` (an existing admin-tier, "operational,
    not a domain-judgment call" permission -- see `identity/rbac.py`'s
    own docstring for that characterization) rather than a new
    permission, since this dashboard introduces no new RBAC concept."""
    user, _, tenant_id = triple
    settings = get_settings()
    databases = []
    for config in settings.databases_for_tenant(tenant_id):
        engine = get_read_only_engine(config)
        tables = refresh_schema_index(engine, config.name, settings=settings)
        diff = get_last_discovery_diff(config.name, settings)
        databases.append(
            SchemaRefreshResult(
                database=config.name,
                table_count=len(tables),
                view_count=sum(1 for table in tables if table.is_view),
                added_tables=list(diff.added_tables) if diff else [],
                removed_tables=list(diff.removed_tables) if diff else [],
                changed_tables=list(diff.changed_tables) if diff else [],
                last_discovered_at=diff.last_discovered_at if diff else None,
            )
        )
    log_security_event(
        "tenant_admin_schema_refresh",
        "info",
        "A tenant admin refreshed their own tenant's database schema index.",
        tenant_id=tenant_id,
        actor_user_id=str(user.id),
        database_count=len(databases),
    )
    return SchemaRefreshResponse(databases=databases)


@router.get("/users", response_model=list[PlatformUserOut])
def list_tenant_users(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> list[PlatformUserOut]:
    _, session, tenant_id = triple
    accounts = list_users(session, tenant_id=tenant_id)
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


def _require_same_tenant_user(session: Session, tenant_id: str, user_id: uuid.UUID) -> User:
    """Resolves `user_id`, 404ing both for a genuinely nonexistent account
    and for one that exists in a *different* tenant -- the identical
    anti-enumeration posture `api/semantic_catalog.py`/`api/onboarding.py`
    already established (a cross-tenant denial must look exactly like
    "not found," never a distinguishable 403)."""
    target = get_user_by_id(session, user_id)
    if target is None or target.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return target


@router.post("/users/{user_id}/roles", response_model=PlatformUserOut)
def assign_tenant_user_role(
    user_id: uuid.UUID,
    payload: AssignRoleRequest,
    triple: tuple[User, Session, str] = Depends(_require_tenant),
    _permission_check: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.USERS_ASSIGN_ROLES)
    ),
) -> PlatformUserOut:
    user, session, tenant_id = triple
    if payload.role_name in _RESTRICTED_ROLE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"The {payload.role_name!r} role cannot be assigned from this dashboard.",
        )
    target = _require_same_tenant_user(session, tenant_id, user_id)
    try:
        _assign_role(
            session, user_id=target.id, role_name=payload.role_name, assigned_by_user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    log_security_event(
        "tenant_admin_role_assigned",
        "info",
        "A tenant admin assigned a role to an account in their own tenant.",
        tenant_id=tenant_id,
        actor_user_id=str(user.id),
        subject_user_id=str(target.id),
        role_name=payload.role_name,
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
def remove_tenant_user_role(
    user_id: uuid.UUID,
    role_name: str,
    triple: tuple[User, Session, str] = Depends(_require_tenant),
    _permission_check: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.USERS_ASSIGN_ROLES)
    ),
) -> PlatformUserOut:
    user, session, tenant_id = triple
    target = _require_same_tenant_user(session, tenant_id, user_id)
    _remove_role(session, user_id=target.id, role_name=role_name)
    log_security_event(
        "tenant_admin_role_removed",
        "info",
        "A tenant admin removed a role from an account in their own tenant.",
        tenant_id=tenant_id,
        actor_user_id=str(user.id),
        subject_user_id=str(target.id),
        role_name=role_name,
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
def list_tenant_assignable_roles(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> list[RoleOut]:
    """Every role *except* `platform_admin` -- a tenant admin has no
    legitimate reason to see or assign a role whose entire purpose is
    cross-tenant access (see `_RESTRICTED_ROLE_NAMES`)."""
    _, session, _ = triple
    return [
        RoleOut(
            name=entry.role.name,
            description=entry.role.description,
            is_system_role=entry.role.is_system_role,
            permissions=list(entry.permissions),
        )
        for entry in list_roles_with_permissions(session)
        if entry.role.name not in _RESTRICTED_ROLE_NAMES
    ]


@router.get("/semantic-catalog-status", response_model=SemanticCatalogStatusOut)
def get_tenant_semantic_catalog_status(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> SemanticCatalogStatusOut:
    _, session, tenant_id = triple
    return SemanticCatalogStatusOut(
        draft_count=len(list_entries(session, tenant_id, status="draft")),
        reviewed_count=len(list_entries(session, tenant_id, status="reviewed")),
        published_count=len(list_entries(session, tenant_id, status="published")),
        superseded_count=len(list_entries(session, tenant_id, status="superseded")),
    )


@router.get("/pending-reviews", response_model=PendingReviewsOut)
def get_tenant_pending_reviews(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> PendingReviewsOut:
    """Counts only -- an operator drills into a specific job's own review
    items on the existing `/db-onboarding` page, or a specific catalog
    entry on `/semantic-review`, both of which already scope correctly to
    this same tenant."""
    _, session, tenant_id = triple
    jobs = list_jobs_for_tenant(session, tenant_id)
    onboarding_pending: dict[str, int] = {}
    for job in jobs:
        for item in _list_review_items(session, job.id, decision="pending"):
            onboarding_pending[item.item_type] = onboarding_pending.get(item.item_type, 0) + 1
    catalog_pending = len(list_entries(session, tenant_id, status="draft")) + len(
        list_entries(session, tenant_id, status="reviewed")
    )
    return PendingReviewsOut(
        onboarding_pending_by_type=onboarding_pending,
        catalog_pending_count=catalog_pending,
        total_pending=sum(onboarding_pending.values()) + catalog_pending,
    )


@router.get("/golden-questions", response_model=list[GoldenQuestionsSummaryOut])
def list_tenant_golden_questions(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> list[GoldenQuestionsSummaryOut]:
    _, session, tenant_id = triple
    results = []
    for job in list_jobs_for_tenant(session, tenant_id):
        for artifact in list_artifacts(session, job.id, artifact_type="golden_questions"):
            questions = artifact.content.get("questions") if artifact.content else None
            results.append(
                GoldenQuestionsSummaryOut(
                    job_id=job.id,
                    database_label=job.database_label,
                    question_count=len(questions) if isinstance(questions, list) else 0,
                )
            )
    return results


@router.get("/evaluation", response_model=list[EvaluationSummaryOut])
def list_tenant_evaluation_results(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> list[EvaluationSummaryOut]:
    _, session, tenant_id = triple
    results = []
    for job in list_jobs_for_tenant(session, tenant_id):
        for artifact in list_artifacts(session, job.id, artifact_type="evaluation_report"):
            content = artifact.content or {}
            results.append(
                EvaluationSummaryOut(
                    job_id=job.id,
                    database_label=job.database_label,
                    pass_count=int(content.get("pass_count", 0)),
                    total_count=int(content.get("total_count", 0)),
                    evaluated_at=artifact.created_at,
                )
            )
    return results


@router.get("/audit", response_model=list[SecurityEventOut])
def list_tenant_audit_events(
    triple: tuple[User, Session, str] = Depends(_require_dashboard_read),
) -> list[SecurityEventOut]:
    """Security events scoped to this tenant only -- reuses the exact same
    in-process ring buffer `api.platform_admin.platform_security_events`
    reads (`security.audit_log.recent_security_events`), filtered here to
    `event["tenant_id"] == this caller's own tenant`. `recent_security_events`
    itself takes no tenant parameter (it is a cross-cutting, platform-wide
    buffer by nature), so the filtering happens in this route, never by
    trusting the buffer to have scoped itself."""
    _, _, tenant_id = triple
    events = recent_security_events(limit=1000)
    return [SecurityEventOut(**event) for event in events if event["tenant_id"] == tenant_id]
