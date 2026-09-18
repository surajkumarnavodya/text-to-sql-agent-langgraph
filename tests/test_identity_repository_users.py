"""Unit tests for identity/repositories/users.py -- against a real
in-memory SQLite database (this module's own docstring explains why: a
real SQLAlchemy round-trip through joins/constraints is a stronger test
than mocking every ORM call, and SQLite is this repo's own testing-only
engine for the identity database -- see identity/models.py's module
docstring). Production always runs against PostgreSQL.
"""

from __future__ import annotations

import pytest
from identity.exceptions import AccountLockedError, DuplicateUserError
from identity.models import Base
from identity.repositories.users import (
    assign_role,
    change_password,
    check_not_locked,
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_user_permissions,
    get_user_roles,
    normalize_email,
    record_login_failure,
    record_login_success,
    remove_role,
)
from identity.security import verify_password
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
def seeded_session(db_session: Session) -> Session:
    """A session with RBAC seed data already loaded -- most user-repository
    tests need at least the "user" role to exist."""
    from identity.bootstrap import seed_rbac

    seed_rbac(db_session)
    return db_session


class TestNormalizeEmail:
    def test_lowercases_and_strips(self):
        assert normalize_email("  Foo@Example.COM  ") == "foo@example.com"


class TestCreateUser:
    def test_creates_user_with_hashed_password(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        assert user.password_hash != "hunter2hunter2"
        assert verify_password("hunter2hunter2", user.password_hash)

    def test_normalizes_email_on_create(self, seeded_session):
        user = create_user(seeded_session, email="Alice@Example.COM", password="hunter2hunter2")
        assert user.email == "alice@example.com"

    def test_assigns_default_role(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        assert get_user_roles(seeded_session, user.id) == ("user",)

    def test_duplicate_email_raises(self, seeded_session):
        create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        with pytest.raises(DuplicateUserError):
            create_user(seeded_session, email="ALICE@example.com", password="anotherpassword1")

    def test_missing_role_does_not_fail_user_creation(self, db_session):
        """No RBAC seed data loaded at all -- user creation must still
        succeed (with no roles), never fail just because the roles table
        is empty."""
        user = create_user(db_session, email="alice@example.com", password="hunter2hunter2")
        assert get_user_roles(db_session, user.id) == ()


class TestLookups:
    def test_get_user_by_email_is_case_insensitive(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        assert get_user_by_email(seeded_session, "ALICE@EXAMPLE.COM").id == user.id

    def test_get_user_by_email_returns_none_when_missing(self, seeded_session):
        assert get_user_by_email(seeded_session, "nobody@example.com") is None

    def test_get_user_by_id_returns_none_when_missing(self, seeded_session):
        import uuid

        assert get_user_by_id(seeded_session, uuid.uuid4()) is None


class TestRolesAndPermissions:
    def test_assign_role_is_idempotent(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        assign_role(seeded_session, user_id=user.id, role_name="admin", assigned_by_user_id=None)
        assign_role(seeded_session, user_id=user.id, role_name="admin", assigned_by_user_id=None)
        assert get_user_roles(seeded_session, user.id).count("admin") == 1

    def test_assign_unknown_role_raises(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        with pytest.raises(ValueError):
            assign_role(
                seeded_session,
                user_id=user.id,
                role_name="not-a-real-role",
                assigned_by_user_id=None,
            )

    def test_remove_role(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        assign_role(seeded_session, user_id=user.id, role_name="admin", assigned_by_user_id=None)
        remove_role(seeded_session, user_id=user.id, role_name="admin")
        assert "admin" not in get_user_roles(seeded_session, user.id)

    def test_admin_gets_more_permissions_than_user(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        user_perms = get_user_permissions(seeded_session, user.id)
        assign_role(seeded_session, user_id=user.id, role_name="admin", assigned_by_user_id=None)
        admin_perms = get_user_permissions(seeded_session, user.id)
        assert admin_perms >= user_perms
        assert "users.read" in admin_perms
        assert "users.read" not in user_perms


class TestLockout:
    def test_check_not_locked_passes_for_fresh_account(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        check_not_locked(user)  # must not raise

    def test_locks_after_max_attempts(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        just_locked = False
        for _ in range(5):
            just_locked = record_login_failure(
                seeded_session, user, max_attempts=5, lockout_minutes=15
            )
        assert just_locked is True
        with pytest.raises(AccountLockedError):
            check_not_locked(user)

    def test_does_not_lock_before_max_attempts(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        for _ in range(4):
            just_locked = record_login_failure(
                seeded_session, user, max_attempts=5, lockout_minutes=15
            )
        assert just_locked is False
        check_not_locked(user)  # must not raise

    def test_success_resets_failed_count_and_unlocks(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        for _ in range(5):
            record_login_failure(seeded_session, user, max_attempts=5, lockout_minutes=15)
        record_login_success(seeded_session, user)
        assert user.failed_login_count == 0
        check_not_locked(user)  # must not raise


class TestChangePassword:
    def test_updates_hash_and_timestamp(self, seeded_session):
        user = create_user(seeded_session, email="alice@example.com", password="hunter2hunter2")
        old_hash = user.password_hash
        change_password(seeded_session, user, "a-brand-new-password")
        assert user.password_hash != old_hash
        assert verify_password("a-brand-new-password", user.password_hash)
        assert not verify_password("hunter2hunter2", user.password_hash)
