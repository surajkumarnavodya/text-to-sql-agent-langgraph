"""Repository layer for `identity.models.AuditLog` -- Prompt 28
(`28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`).

**A real, found-and-fixed gap, not a pre-existing feature being
extended**: `AuditLog` has existed as a real, migrated table since this
project's identity schema was first built, with `Permission.AUDIT_READ_ANY`/
`AUDIT_READ_OWN` seeded and granted since then too -- but nothing in this
codebase has ever written a row into it, and no route has ever read one
back. This module is the first real write/read path. `identity.share_audit
.record_share_event`'s own docstring already describes the intended
convention ("a queryable DB row alongside `security.audit_log
.log_security_event`'s structured log line") -- this module is that same
convention applied generically, rather than one more feature-specific
table (`ShareAuditEvent`) for every future admin action.

**Deliberately scoped to this prompt's own new mutating actions only**
(`api/platform_admin.py`'s tenant suspend/activate and role assign/
remove) -- retrofitting every existing mutating route across this whole
application to also write an `AuditLog` row would be a much larger,
separate undertaking, named as a known limitation rather than attempted
here. `security.audit_log.log_security_event`'s own structured-logging
stream (now also feeding `recent_security_events`, see that module) is
unaffected and remains the broader, already-established mechanism for
every other security-relevant event in this codebase.

**No tenant_id column exists on this table** (`identity.models.AuditLog`
predates Prompt 20's tenant boundary and was never retrofitted) -- a
disclosed, structural limitation: `list_audit_events` cannot be scoped to
one tenant, only filtered by actor/subject/action/resource/outcome. Its
only caller (`api/platform_admin.py`) is already `PLATFORM_ADMIN`-gated
for cross-tenant visibility, so this is not a new leak, but it does mean
this table could never become the backing store for a future *tenant*-
scoped audit view without a migration first.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from identity.models import AuditLog

logger = logging.getLogger(__name__)

AuditOutcome = Literal["success", "failure", "denied"]


def record_audit_event(
    session: Session,
    *,
    actor_user_id: uuid.UUID | None,
    subject_user_id: uuid.UUID | None = None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: AuditOutcome,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    """Writes one `AuditLog` row. Never raises -- an audit-logging failure
    must not fail the request it describes, the exact same fail-open
    posture `identity.share_audit.record_share_event` already applies for
    its own (narrower) table.

    `metadata` must never contain a raw secret/credential/token -- callers
    in this codebase only ever pass already-safe scalars (a tenant id, a
    role name, a status value), the same discipline
    `identity.share_audit.record_share_event`'s own docstring requires of
    its callers.
    """
    try:
        session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                subject_user_id=subject_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                metadata_json=metadata,
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 - audit logging must never fail the caller
        session.rollback()
        logger.warning("[audit] failed to persist AuditLog row for action=%r", action)


def list_audit_events(
    session: Session,
    *,
    action: str | None = None,
    resource_type: str | None = None,
    outcome: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    subject_user_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[AuditLog]:
    """Newest-first, optionally filtered -- `GET /platform-admin/audit-logs`'s
    own query. See this module's own docstring for why there is no
    `tenant_id` filter: the underlying table has no such column."""
    statement = select(AuditLog)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    if resource_type is not None:
        statement = statement.where(AuditLog.resource_type == resource_type)
    if outcome is not None:
        statement = statement.where(AuditLog.outcome == outcome)
    if actor_user_id is not None:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    if subject_user_id is not None:
        statement = statement.where(AuditLog.subject_user_id == subject_user_id)
    statement = statement.order_by(AuditLog.created_at.desc()).limit(limit)
    return list(session.scalars(statement))
