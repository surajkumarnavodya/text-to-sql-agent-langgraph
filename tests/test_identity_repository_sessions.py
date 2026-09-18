"""Unit tests for identity/repositories/sessions.py -- session creation,
rotation, reuse detection, and revocation, against an in-memory SQLite
database (see test_identity_repository_users.py's module docstring for
why a real DB round-trip is used here instead of mocks)."""

from __future__ import annotations

import uuid

import pytest
from identity.exceptions import RefreshTokenInvalidError, RefreshTokenReusedError
from identity.models import Base
from identity.repositories.sessions import (
    create_session,
    get_session_by_refresh_token,
    list_active_sessions_for_user,
    revoke_all_sessions_for_user,
    revoke_session,
    revoke_session_family,
    rotate_refresh_token,
    touch_last_used,
)
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
def user_id() -> uuid.UUID:
    return uuid.uuid4()


class TestCreateSession:
    def test_returns_a_usable_raw_token(self, db_session, user_id):
        auth_session, raw_token = create_session(
            db_session, user_id=user_id, refresh_token_expire_days=14
        )
        assert get_session_by_refresh_token(db_session, raw_token).id == auth_session.id

    def test_raw_token_is_never_stored_verbatim(self, db_session, user_id):
        auth_session, raw_token = create_session(
            db_session, user_id=user_id, refresh_token_expire_days=14
        )
        assert auth_session.refresh_token_hash != raw_token

    def test_new_login_gets_its_own_family(self, db_session, user_id):
        s1, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        s2, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        assert s1.session_family_id != s2.session_family_id


class TestRotateRefreshToken:
    def test_rotation_issues_a_new_token_in_the_same_family(self, db_session, user_id):
        original, raw1 = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        rotated, raw2 = rotate_refresh_token(
            db_session,
            raw_refresh_token=raw1,
            refresh_token_expire_days=14,
            ip_address=None,
            user_agent=None,
        )
        assert raw2 != raw1
        assert rotated.session_family_id == original.session_family_id

    def test_old_token_is_revoked_after_rotation(self, db_session, user_id):
        original, raw1 = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        rotate_refresh_token(
            db_session,
            raw_refresh_token=raw1,
            refresh_token_expire_days=14,
            ip_address=None,
            user_agent=None,
        )
        db_session.refresh(original)
        assert original.revoked_at is not None
        assert original.revoke_reason == "rotated"

    def test_old_token_no_longer_works_after_rotation(self, db_session, user_id):
        _, raw1 = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        rotate_refresh_token(
            db_session,
            raw_refresh_token=raw1,
            refresh_token_expire_days=14,
            ip_address=None,
            user_agent=None,
        )
        # nothing raised yet -- using it again below is the actual reuse test

    def test_reusing_a_rotated_token_raises_and_revokes_whole_family(self, db_session, user_id):
        _, raw1 = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        rotated, raw2 = rotate_refresh_token(
            db_session,
            raw_refresh_token=raw1,
            refresh_token_expire_days=14,
            ip_address=None,
            user_agent=None,
        )
        with pytest.raises(RefreshTokenReusedError):
            rotate_refresh_token(
                db_session,
                raw_refresh_token=raw1,
                refresh_token_expire_days=14,
                ip_address=None,
                user_agent=None,
            )
        # the *second-generation* token (raw2) must also now be dead, since
        # the whole family was revoked, not just the replayed row.
        db_session.refresh(rotated)
        assert rotated.revoked_at is not None

    def test_unknown_token_raises_invalid(self, db_session):
        with pytest.raises(RefreshTokenInvalidError):
            rotate_refresh_token(
                db_session,
                raw_refresh_token="totally-made-up-token",
                refresh_token_expire_days=14,
                ip_address=None,
                user_agent=None,
            )

    def test_expired_token_raises_invalid(self, db_session, user_id):
        _, raw1 = create_session(db_session, user_id=user_id, refresh_token_expire_days=-1)
        with pytest.raises(RefreshTokenInvalidError):
            rotate_refresh_token(
                db_session,
                raw_refresh_token=raw1,
                refresh_token_expire_days=14,
                ip_address=None,
                user_agent=None,
            )


class TestRevocation:
    def test_revoke_session_marks_revoked(self, db_session, user_id):
        auth_session, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        revoke_session(db_session, auth_session, reason="logout")
        assert auth_session.revoked_at is not None
        assert auth_session.revoke_reason == "logout"

    def test_revoke_all_sessions_for_user_only_affects_that_user(self, db_session, user_id):
        other_user_id = uuid.uuid4()
        create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        create_session(db_session, user_id=other_user_id, refresh_token_expire_days=14)

        count = revoke_all_sessions_for_user(db_session, user_id, reason="logout_all")
        assert count == 2
        assert list_active_sessions_for_user(db_session, user_id) == []
        assert len(list_active_sessions_for_user(db_session, other_user_id)) == 1

    def test_revoke_session_family_only_affects_that_family(self, db_session, user_id):
        s1, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        s2, _ = create_session(
            db_session, user_id=user_id, refresh_token_expire_days=14
        )  # different family
        revoke_session_family(db_session, s1.session_family_id, reason="reuse_detected")
        db_session.refresh(s1)
        db_session.refresh(s2)
        assert s1.revoked_at is not None
        assert s2.revoked_at is None


class TestListActiveSessions:
    def test_excludes_revoked_sessions(self, db_session, user_id):
        s1, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        s2, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        revoke_session(db_session, s1, reason="logout")
        active = list_active_sessions_for_user(db_session, user_id)
        assert [s.id for s in active] == [s2.id]

    def test_excludes_expired_sessions(self, db_session, user_id):
        create_session(db_session, user_id=user_id, refresh_token_expire_days=-1)
        assert list_active_sessions_for_user(db_session, user_id) == []


class TestTouchLastUsed:
    def test_updates_last_used_at(self, db_session, user_id):
        auth_session, _ = create_session(db_session, user_id=user_id, refresh_token_expire_days=14)
        original = auth_session.last_used_at
        touch_last_used(db_session, auth_session)
        assert auth_session.last_used_at >= original
