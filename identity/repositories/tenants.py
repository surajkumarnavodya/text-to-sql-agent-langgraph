"""Tenant rows -- plain CRUD for `identity.models.Tenant`, zero
authorization logic.

Mirrors `identity/repositories/onboarding.py`'s and
`semantic_catalog.py`'s own split exactly: this module never decides *who*
may read or write a tenant, only how. Every authorization/ABAC decision
about tenancy lives in `security/tenancy.py` (resolution + fail-closed
status enforcement) and the four existing deny-by-default policy modules
that compare tenant ids (`identity/share_policy.py`,
`onboarding/policy.py`, `semantic/catalog_policy.py`,
`recommendation/governance_policy.py`).

Prompt 20 (`20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from identity.models import Tenant

logger = logging.getLogger(__name__)

#: Legal `Tenant.status` values -- must match the `Enum(...)` on the column
#: itself (`identity.models.Tenant.status`). Only `"active"` grants access;
#: every other value fails closed at `security.tenancy
#: .resolve_tenant_context`, so adding a new state here is deliberately a
#: deny-by-default change, never an accidentally-permissive one.
TENANT_STATUSES: frozenset[str] = frozenset({"active", "suspended"})


def get_tenant(session: Session, tenant_id: str) -> Tenant | None:
    """Returns the tenant row for `tenant_id`, including a soft-deleted one.

    Soft-deleted rows are deliberately *not* filtered out here: the caller
    that actually cares (`security.tenancy.resolve_tenant_context`) must
    be able to distinguish "this tenant was deleted" from "this tenant was
    never created," and both of those fail closed there anyway. A
    repository function that silently hid deleted rows would make that
    distinction impossible to draw.
    """
    return session.scalar(select(Tenant).where(Tenant.id == tenant_id))


def list_tenants(session: Session, *, include_deleted: bool = False) -> list[Tenant]:
    """Every tenant, oldest first. Administrative/diagnostic use only --
    no route exposes this to a non-admin caller."""
    statement = select(Tenant).order_by(Tenant.created_at, Tenant.id)
    if not include_deleted:
        statement = statement.where(Tenant.deleted_at.is_(None))
    return list(session.scalars(statement))


def create_tenant(
    session: Session,
    *,
    tenant_id: str,
    name: str,
    status: str = "active",
    metadata: dict | None = None,
) -> Tenant:
    """Creates one tenant. Raises `ValueError` on an unknown `status` (the
    column's own `Enum(validate_strings=True)` would also reject it, but
    failing here gives a clearer error before any SQL is emitted)."""
    if status not in TENANT_STATUSES:
        raise ValueError(
            f"Unknown tenant status {status!r}; expected one of {sorted(TENANT_STATUSES)}."
        )
    tenant = Tenant(id=tenant_id, name=name, status=status, metadata_json=metadata)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    logger.info("[tenants] created tenant %r (status=%s)", tenant_id, status)
    return tenant


def set_tenant_status(session: Session, *, tenant_id: str, status: str) -> Tenant | None:
    """Flips a tenant's status, returning the updated row (or `None` if no
    such tenant exists).

    Suspending a tenant takes effect for every one of its users on their
    *next request* -- `security.tenancy.resolve_tenant_context` is
    consulted per request and reads this column live, deliberately rather
    than caching it, so a suspension never waits for an access token to
    expire. See that function's own docstring.
    """
    if status not in TENANT_STATUSES:
        raise ValueError(
            f"Unknown tenant status {status!r}; expected one of {sorted(TENANT_STATUSES)}."
        )
    tenant = get_tenant(session, tenant_id)
    if tenant is None:
        return None
    tenant.status = status
    session.commit()
    session.refresh(tenant)
    logger.info("[tenants] tenant %r status set to %s", tenant_id, status)
    return tenant


def soft_delete_tenant(session: Session, *, tenant_id: str) -> Tenant | None:
    """Marks a tenant deleted without removing any of its data.

    Soft delete only, matching `Conversation`/`ConversationShare`'s own
    retention posture: the `RESTRICT` foreign keys on `users.tenant_id`/
    `conversations.tenant_id` mean a hard delete would fail anyway while
    any row still references it, and silently cascading a tenant deletion
    across every table that carries its id is exactly the kind of
    destructive operation this project requires explicit approval for.
    """
    tenant = get_tenant(session, tenant_id)
    if tenant is None:
        return None
    tenant.deleted_at = datetime.now(UTC)
    session.commit()
    session.refresh(tenant)
    logger.info("[tenants] tenant %r soft-deleted", tenant_id)
    return tenant
