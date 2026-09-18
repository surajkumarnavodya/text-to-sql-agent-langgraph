"""Sign-in/sign-out history (`identity.models.SigninEvent`).

One small, dedicated module rather than folded into `users.py`/`sessions.py`
-- every other repository module here is scoped to one table/concern, and
sign-in events are written from several different call sites (register,
login success/failure, logout, refresh, lockout) that don't otherwise share
code.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from identity.models import SigninEvent


def record_signin_event(
    session: Session,
    *,
    event_type: str,
    success: bool,
    user_id: uuid.UUID | None = None,
    identifier_attempted: str | None = None,
    failure_reason_code: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    session_id: uuid.UUID | None = None,
) -> None:
    """Appends one `signin_events` row. Never raises on a logging-shaped
    failure path -- callers (`api/identity_auth.py`) call this *after* the
    real security decision has already been made (lock/reject/allow), so a
    write failure here must not change that outcome; wrap in try/except at
    the call site if you need that guarantee, same posture
    `security.audit_log.log_security_event` already has for its own event
    stream.
    """
    session.add(
        SigninEvent(
            user_id=user_id,
            identifier_attempted=identifier_attempted,
            event_type=event_type,
            success=success,
            failure_reason_code=failure_reason_code,
            ip_address=ip_address,
            user_agent=user_agent,
            session_id=session_id,
        )
    )
    session.commit()
