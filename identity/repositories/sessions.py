"""Refresh-token sessions: creation, rotation-with-reuse-detection, and
revocation.

`AuthSession.refresh_token_hash` is the only thing ever persisted for a
refresh token -- see `identity.security.hash_refresh_token`'s own
docstring for why SHA-256 (not Argon2id) is the right, standard choice
here. Every function below takes the *raw* token only long enough to hash
it; nothing in this module ever stores or logs the raw value.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from identity.exceptions import RefreshTokenInvalidError, RefreshTokenReusedError
from identity.models import AuthSession
from identity.security import generate_refresh_token, hash_refresh_token


def _aware_utc(value: datetime | None) -> datetime | None:
    """See `identity.repositories.users._aware_utc`'s docstring -- same
    SQLite-vs-PostgreSQL timezone-round-tripping normalization, needed
    here for `AuthSession.expires_at`/`revoked_at` reads."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def create_session(
    session: Session,
    *,
    user_id: uuid.UUID,
    refresh_token_expire_days: int,
    device_name: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    session_family_id: uuid.UUID | None = None,
) -> tuple[AuthSession, str]:
    """Creates a brand-new session (a fresh `session_family_id` if none is
    given -- i.e. a new login, not a rotation) and returns
    `(auth_session, raw_refresh_token)`. The raw token is returned exactly
    once, here -- callers must hand it to the client (a `Set-Cookie`
    header, never a JSON body field) and never persist it themselves.
    """
    now = datetime.now(UTC)
    raw_token = generate_refresh_token()
    auth_session = AuthSession(
        user_id=user_id,
        session_family_id=session_family_id or uuid.uuid4(),
        refresh_token_hash=hash_refresh_token(raw_token),
        device_name=device_name,
        user_agent=user_agent,
        ip_address=ip_address,
        last_used_at=now,
        expires_at=now + timedelta(days=refresh_token_expire_days),
    )
    session.add(auth_session)
    session.commit()
    session.refresh(auth_session)
    return auth_session, raw_token


def get_session_by_refresh_token(session: Session, raw_refresh_token: str) -> AuthSession | None:
    token_hash = hash_refresh_token(raw_refresh_token)
    return session.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == token_hash))


def rotate_refresh_token(
    session: Session,
    *,
    raw_refresh_token: str,
    refresh_token_expire_days: int,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[AuthSession, str]:
    """Validates `raw_refresh_token`, then rotates it: issues a brand-new
    opaque refresh token in the *same* `session_family_id`, marks the old
    row revoked (`revoke_reason="rotated"`, `replaced_by_session_id` set to
    the new row's id), and returns `(new_auth_session, new_raw_token)`.

    Raises:
        RefreshTokenInvalidError: no session matches this token's hash at
            all, or it matches one that's expired.
        RefreshTokenReusedError: the token matches a session that's
            *already* revoked -- a real reuse signal (the token was already
            rotated once, or explicitly logged out, and is being replayed).
            Per this feature's own requirement, this revokes the **entire**
            session family, not just the one matched row, since a replayed
            token is treated as evidence the whole lineage may be
            compromised.
    """
    existing = get_session_by_refresh_token(session, raw_refresh_token)
    if existing is None:
        raise RefreshTokenInvalidError("No session matches this refresh token.")

    now = datetime.now(UTC)
    if existing.revoked_at is not None:
        revoke_session_family(session, existing.session_family_id, reason="reuse_detected")
        raise RefreshTokenReusedError(
            f"Refresh token for session {existing.id} was already revoked/rotated -- "
            "possible token reuse. Revoking session family "
            f"{existing.session_family_id}."
        )
    if _aware_utc(existing.expires_at) <= now:
        raise RefreshTokenInvalidError(f"Refresh token for session {existing.id} has expired.")

    new_session, new_raw_token = create_session(
        session,
        user_id=existing.user_id,
        refresh_token_expire_days=refresh_token_expire_days,
        device_name=existing.device_name,
        user_agent=user_agent or existing.user_agent,
        ip_address=ip_address or existing.ip_address,
        session_family_id=existing.session_family_id,
    )
    existing.revoked_at = now
    existing.revoke_reason = "rotated"
    existing.replaced_by_session_id = new_session.id
    session.commit()
    return new_session, new_raw_token


def touch_last_used(session: Session, auth_session: AuthSession) -> None:
    auth_session.last_used_at = datetime.now(UTC)
    session.commit()


def revoke_session(session: Session, auth_session: AuthSession, *, reason: str) -> None:
    auth_session.revoked_at = datetime.now(UTC)
    auth_session.revoke_reason = reason
    session.commit()


def revoke_session_family(session: Session, session_family_id: uuid.UUID, *, reason: str) -> None:
    """Revokes every still-live session sharing `session_family_id` --
    used both for reuse-detection (`rotate_refresh_token` above) and
    nothing else in Phase A; Phase B/D routes (`POST /auth/logout-all`,
    admin session revocation) call `revoke_all_sessions_for_user` instead,
    which spans every family a user has, not just one lineage."""
    session.execute(
        update(AuthSession)
        .where(AuthSession.session_family_id == session_family_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoke_reason=reason)
    )
    session.commit()


def revoke_all_sessions_for_user(session: Session, user_id: uuid.UUID, *, reason: str) -> int:
    """Revokes every still-live session for `user_id` -- `POST
    /auth/logout-all` and an admin's `POST /admin/users/{id}/revoke-all-sessions`
    both call this. Returns the number of sessions actually revoked."""
    result = session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoke_reason=reason)
    )
    session.commit()
    return result.rowcount


def list_active_sessions_for_user(session: Session, user_id: uuid.UUID) -> list[AuthSession]:
    now = datetime.now(UTC)
    return list(
        session.scalars(
            select(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
            .order_by(AuthSession.last_used_at.desc())
        ).all()
    )
