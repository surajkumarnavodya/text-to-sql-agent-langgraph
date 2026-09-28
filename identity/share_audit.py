"""Privacy-safe audit trail for the sharing feature -- writes both a
queryable `ShareAuditEvent` row (`identity.models`) and a structured log
line via `security.audit_log.log_security_event`, the same dual
DB-row-plus-log convention this codebase already uses for other durable
audit trails (`identity.models.AuditLog` alongside `security/oidc.py`'s own
`log_security_event` calls).

**Every call site in this feature routes through `record_share_event`
below -- there is no other place a `ShareAuditEvent` row is ever created.**
This is what makes the "never log a raw token/cookie/private content"
guarantee enforceable in one place instead of trusted to every call site
individually: `safe_metadata` accepts only the allowlisted, already-safe
key/value pairs this module's own callers pass (share id, conversation id,
event-specific counts/flags), never a raw request body or model instance.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from sqlalchemy.orm import Session

from identity.models import ShareAuditEvent
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)

#: Every event type this feature ever emits -- kept as one explicit tuple
#: (not "whatever string a caller happens to pass") so a typo can be caught
#: by a test asserting every call site uses a value from this set, and so
#: this module's own docstring/`docs/SHARING_SECURITY.md` stays the
#: authoritative list of what an operator can filter/alert on.
SHARE_EVENT_TYPES: tuple[str, ...] = (
    "share_created",
    "share_updated",
    "share_revoked",
    "link_regenerated",
    "member_invited",
    "member_removed",
    "invitation_accepted",
    "invitation_rejected",
    "view_allowed",
    "view_denied",
    "download_allowed",
    "download_denied",
    "idor_attempt",
    "rate_limited",
)


def record_share_event(
    session: Session,
    *,
    share_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    event_type: str,
    result: str,
    reason: str | None = None,
    request_id: str | None = None,
    safe_metadata: dict[str, Any] | None = None,
) -> None:
    """Writes one `ShareAuditEvent` row and mirrors it to the structured
    security log.

    `safe_metadata` must never contain a raw token, cookie, Authorization
    header value, or private message/attachment content -- every caller in
    this feature only ever passes small, already-safe scalars (counts,
    booleans, role names, the *hashed* form of anything token-shaped). This
    function does not itself scan/redact the dict -- it trusts its callers,
    all of which live in this same feature and are covered by
    `tests/test_share_audit.py`'s own "never logs a raw token" assertions.

    Never raises: an audit-logging failure must not fail the request it's
    describing (the same fail-open posture this codebase already applies to
    every other non-critical-path observability write, e.g.
    `observability.metrics.PerformanceMetrics`'s own try/except around
    `agent.graph.run_agent`'s recording call).
    """
    try:
        session.add(
            ShareAuditEvent(
                share_id=share_id,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                event_type=event_type,
                result=result,
                reason=reason,
                request_id=request_id,
                safe_metadata=safe_metadata,
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 - audit logging must never fail the caller
        session.rollback()
        logger.warning("[share_audit] failed to persist ShareAuditEvent for %s", event_type)

    severity: Literal["info", "warning"] = "warning" if result in ("denied", "error") else "info"
    log_security_event(
        f"share_{event_type}" if not event_type.startswith("share_") else event_type,
        severity,
        f"Conversation-sharing event: {event_type} -> {result}.",
        share_id=str(share_id) if share_id else None,
        conversation_id=str(conversation_id) if conversation_id else None,
        actor_user_id=str(actor_user_id) if actor_user_id else None,
        result=result,
        reason=reason,
        request_id=request_id,
    )
