"""Password-reset and email-verification tokens -- both opaque, high-entropy
random strings, hashed at rest, single-use. Reuses `identity.security
.generate_refresh_token`/`hash_refresh_token` directly rather than a
second, parallel implementation -- the underlying requirement (an
unguessable random value, never stored raw, hashed with a fast,
collision-resistant hash) is identical for a refresh token, a password-reset
link, and an email-verification link; only the table and TTL differ.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from identity.exceptions import InvalidTokenError, TokenAlreadyUsedError, TokenExpiredError
from identity.models import EmailVerificationToken, PasswordResetToken, User
from identity.security import generate_refresh_token as generate_token
from identity.security import hash_refresh_token as hash_token


def _aware_utc(value: datetime | None) -> datetime | None:
    """See `identity.repositories.users._aware_utc`'s docstring -- same
    SQLite-vs-PostgreSQL timezone-round-tripping normalization."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def create_password_reset_token(
    session: Session, user_id: uuid.UUID, *, expire_minutes: int
) -> str:
    """Creates a new reset token for `user_id` and returns the *raw* value
    -- callers (`api/identity_auth.py`) hand this to `identity.email`, never
    persist it themselves. Does not invalidate any prior unused token for
    the same user -- multiple valid reset links can coexist (e.g. the user
    requested one, didn't see the email, requested another); each is
    independently single-use.
    """
    raw_token = generate_token()
    session.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
        )
    )
    session.commit()
    return raw_token


def redeem_password_reset_token(session: Session, raw_token: str) -> User:
    """Validates and consumes a password-reset token, returning the
    matching `User`. Marks the token `used_at` in the same transaction as
    the validation, so a raced double-redemption of the same token can
    only ever succeed once (the second call finds `used_at` already set).

    Raises:
        InvalidTokenError: no token matches this value at all.
        TokenExpiredError: the token existed but is past `expires_at`.
        TokenAlreadyUsedError: the token was already redeemed once.
    """
    token_hash = hash_token(raw_token)
    row = session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    if row is None:
        raise InvalidTokenError("No password reset token matches this value.")
    if row.used_at is not None:
        raise TokenAlreadyUsedError(f"Password reset token {row.id} was already used.")
    if _aware_utc(row.expires_at) <= datetime.now(UTC):
        raise TokenExpiredError(f"Password reset token {row.id} has expired.")

    row.used_at = datetime.now(UTC)
    user = session.get(User, row.user_id)
    session.commit()
    if user is None:
        raise InvalidTokenError("The account this token belongs to no longer exists.")
    return user


def create_email_verification_token(
    session: Session, user_id: uuid.UUID, *, expire_minutes: int
) -> str:
    raw_token = generate_token()
    session.add(
        EmailVerificationToken(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
        )
    )
    session.commit()
    return raw_token


def redeem_email_verification_token(session: Session, raw_token: str) -> User:
    """Same validation shape as `redeem_password_reset_token` -- see that
    function's docstring. Does **not** itself flip `User.is_email_verified`/
    `status` -- the caller (`api/identity_auth.py::verify_email`) does that,
    since this module stays scoped to token lifecycle only, not account
    state.

    Raises:
        InvalidTokenError, TokenExpiredError, TokenAlreadyUsedError: same
            meanings as `redeem_password_reset_token`.
    """
    token_hash = hash_token(raw_token)
    row = session.scalar(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash)
    )
    if row is None:
        raise InvalidTokenError("No email verification token matches this value.")
    if row.used_at is not None:
        raise TokenAlreadyUsedError(f"Email verification token {row.id} was already used.")
    if _aware_utc(row.expires_at) <= datetime.now(UTC):
        raise TokenExpiredError(f"Email verification token {row.id} has expired.")

    row.used_at = datetime.now(UTC)
    user = session.get(User, row.user_id)
    session.commit()
    if user is None:
        raise InvalidTokenError("The account this token belongs to no longer exists.")
    return user
