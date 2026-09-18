"""Unit tests for identity/repositories/tokens.py -- password-reset and
email-verification token issuance/redemption, against an in-memory SQLite
database (see test_identity_repository_users.py's module docstring for
why)."""

from __future__ import annotations

import pytest
from identity.exceptions import InvalidTokenError, TokenAlreadyUsedError, TokenExpiredError
from identity.models import Base
from identity.repositories.tokens import (
    create_email_verification_token,
    create_password_reset_token,
    redeem_email_verification_token,
    redeem_password_reset_token,
)
from identity.repositories.users import create_user
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def user(db_session):
    return create_user(db_session, email="alice@example.com", password="hunter2hunter2")


class TestPasswordResetTokens:
    def test_redeems_and_returns_the_user(self, db_session, user):
        raw = create_password_reset_token(db_session, user.id, expire_minutes=60)
        redeemed = redeem_password_reset_token(db_session, raw)
        assert redeemed.id == user.id

    def test_raw_token_is_not_stored_verbatim(self, db_session, user):
        from identity.models import PasswordResetToken
        from sqlalchemy import select

        raw = create_password_reset_token(db_session, user.id, expire_minutes=60)
        row = db_session.scalar(
            select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
        assert row.token_hash != raw

    def test_unknown_token_raises_invalid(self, db_session):
        with pytest.raises(InvalidTokenError):
            redeem_password_reset_token(db_session, "not-a-real-token")

    def test_expired_token_raises_expired(self, db_session, user):
        raw = create_password_reset_token(db_session, user.id, expire_minutes=-1)
        with pytest.raises(TokenExpiredError):
            redeem_password_reset_token(db_session, raw)

    def test_reused_token_raises_already_used(self, db_session, user):
        raw = create_password_reset_token(db_session, user.id, expire_minutes=60)
        redeem_password_reset_token(db_session, raw)
        with pytest.raises(TokenAlreadyUsedError):
            redeem_password_reset_token(db_session, raw)

    def test_multiple_outstanding_tokens_are_each_independently_valid(self, db_session, user):
        raw1 = create_password_reset_token(db_session, user.id, expire_minutes=60)
        raw2 = create_password_reset_token(db_session, user.id, expire_minutes=60)
        redeem_password_reset_token(db_session, raw1)
        # raw2 must still work -- creating a second token must not silently
        # invalidate the first caller's still-outstanding one.
        redeemed = redeem_password_reset_token(db_session, raw2)
        assert redeemed.id == user.id


class TestEmailVerificationTokens:
    def test_redeems_and_returns_the_user(self, db_session, user):
        raw = create_email_verification_token(db_session, user.id, expire_minutes=1440)
        redeemed = redeem_email_verification_token(db_session, raw)
        assert redeemed.id == user.id

    def test_does_not_itself_flip_verification_status(self, db_session, user):
        """Token redemption is scoped to token lifecycle only -- flipping
        User.is_email_verified/status is the API route handler's job, per
        this module's own docstring."""
        raw = create_email_verification_token(db_session, user.id, expire_minutes=1440)
        redeem_email_verification_token(db_session, raw)
        assert user.is_email_verified is False

    def test_expired_token_raises_expired(self, db_session, user):
        raw = create_email_verification_token(db_session, user.id, expire_minutes=-1)
        with pytest.raises(TokenExpiredError):
            redeem_email_verification_token(db_session, raw)

    def test_reused_token_raises_already_used(self, db_session, user):
        raw = create_email_verification_token(db_session, user.id, expire_minutes=1440)
        redeem_email_verification_token(db_session, raw)
        with pytest.raises(TokenAlreadyUsedError):
            redeem_email_verification_token(db_session, raw)

    def test_unknown_token_raises_invalid(self, db_session):
        with pytest.raises(InvalidTokenError):
            redeem_email_verification_token(db_session, "not-a-real-token")

    def test_password_reset_token_cannot_redeem_as_verification(self, db_session, user):
        """The two token tables are entirely separate -- a value minted by
        one must never validate against the other."""
        raw = create_password_reset_token(db_session, user.id, expire_minutes=60)
        with pytest.raises(InvalidTokenError):
            redeem_email_verification_token(db_session, raw)
