"""Unit tests for identity/bootstrap.py -- idempotent RBAC seeding and the
first-admin-account bootstrap."""

from __future__ import annotations

import pytest
from identity.bootstrap import bootstrap_admin_user, seed_rbac
from identity.models import Base, Role, RolePermission
from identity.models import Permission as PermissionModel
from identity.repositories.users import get_user_roles
from identity.security import verify_password
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        local_auth_enabled=True,
        auth_database_url="postgresql://placeholder",
        jwt_secret_key="s" * 40,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestSeedRbac:
    def test_creates_every_seed_role_and_permission(self, db_session):
        seed_rbac(db_session)
        from identity.rbac import SEED_PERMISSIONS, SEED_ROLES

        assert db_session.scalar(select(PermissionModel).limit(1)) is not None
        role_names = {r.name for r in db_session.scalars(select(Role)).all()}
        assert role_names == {name for name, *_ in SEED_ROLES}
        permission_codes = {p.code for p in db_session.scalars(select(PermissionModel)).all()}
        assert permission_codes == {code.value for code, _ in SEED_PERMISSIONS}

    def test_is_idempotent(self, db_session):
        seed_rbac(db_session)
        role_count_1 = len(db_session.scalars(select(Role)).all())
        seed_rbac(db_session)
        role_count_2 = len(db_session.scalars(select(Role)).all())
        assert role_count_1 == role_count_2

    def test_does_not_disturb_a_manually_added_extra_grant(self, db_session):
        """Re-running seed_rbac must never reset an operator's own,
        since-added customization -- it only ever adds what's missing."""
        seed_rbac(db_session)
        viewer = db_session.scalar(select(Role).where(Role.name == "viewer"))
        users_read = db_session.scalar(
            select(PermissionModel).where(PermissionModel.code == "users.read")
        )
        db_session.add(RolePermission(role_id=viewer.id, permission_id=users_read.id))
        db_session.commit()

        seed_rbac(db_session)

        still_there = db_session.get(
            RolePermission, {"role_id": viewer.id, "permission_id": users_read.id}
        )
        assert still_there is not None


class TestBootstrapAdminUser:
    def test_noop_when_disabled(self, db_session):
        settings = _settings(bootstrap_admin_enabled=False)
        seed_rbac(db_session)
        assert bootstrap_admin_user(db_session, settings) is None

    def test_creates_admin_with_admin_role(self, db_session):
        seed_rbac(db_session)
        settings = _settings(
            bootstrap_admin_enabled=True,
            bootstrap_admin_email="admin@example.com",
            bootstrap_admin_password="a-strong-password-123",
        )
        user = bootstrap_admin_user(db_session, settings)
        assert user is not None
        assert user.email == "admin@example.com"
        assert get_user_roles(db_session, user.id) == ("admin",)
        assert verify_password("a-strong-password-123", user.password_hash)

    def test_is_idempotent(self, db_session):
        seed_rbac(db_session)
        settings = _settings(
            bootstrap_admin_enabled=True,
            bootstrap_admin_email="admin@example.com",
            bootstrap_admin_password="a-strong-password-123",
        )
        user1 = bootstrap_admin_user(db_session, settings)
        user2 = bootstrap_admin_user(db_session, settings)
        assert user1.id == user2.id

    def test_rerun_does_not_reset_password(self, db_session):
        """Re-running bootstrap (e.g. every app startup) must never
        clobber a password the admin has since changed themselves."""
        seed_rbac(db_session)
        settings = _settings(
            bootstrap_admin_enabled=True,
            bootstrap_admin_email="admin@example.com",
            bootstrap_admin_password="original-password-123",
        )
        user = bootstrap_admin_user(db_session, settings)
        from identity.repositories.users import change_password

        change_password(db_session, user, "the-admin-changed-it-to-this")

        bootstrap_admin_user(db_session, settings)  # re-run with the OLD configured password
        db_session.refresh(user)
        assert verify_password("the-admin-changed-it-to-this", user.password_hash)
        assert not verify_password("original-password-123", user.password_hash)

    def test_raises_if_admin_role_not_seeded_yet(self, db_session):
        settings = _settings(
            bootstrap_admin_enabled=True,
            bootstrap_admin_email="admin@example.com",
            bootstrap_admin_password="a-strong-password-123",
        )
        with pytest.raises(ValueError):
            bootstrap_admin_user(db_session, settings)
