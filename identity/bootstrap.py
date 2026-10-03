"""Idempotent identity-database seeding: the default tenant
(`identity.models.Tenant`), the RBAC seed data (`identity/rbac.py`'s
`SEED_ROLES`/`SEED_PERMISSIONS`) and, optionally, the very first admin
account.

Both operations are safe to call repeatedly (on every app startup, or by
re-running `scripts/bootstrap_admin.py` by hand) -- neither ever creates a
duplicate row, so there is no "only run this once" footgun.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings
from identity.models import Permission as PermissionModel
from identity.models import Role, RolePermission, Tenant, User
from identity.rbac import SEED_PERMISSIONS, SEED_ROLES
from identity.repositories.users import create_user, get_user_by_email
from security.tenancy import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


def ensure_default_tenant(session: Session) -> Tenant:
    """Inserts the `security.tenancy.DEFAULT_TENANT_ID` tenant row if it
    doesn't already exist, returning it either way.

    Must run before any user is created: `users.tenant_id` is a `RESTRICT`
    foreign key onto `tenants.id` defaulting to `"default"`, so on
    PostgreSQL (production) an insert into `users` fails outright without
    this row. The Alembic migration that adds the column
    (`a4c7e2b9d6f5_multi_tenant_boundary`) seeds the same row for an
    already-migrated database; this function is what keeps a freshly
    `Base.metadata.create_all`-built database -- every test, and
    `scripts/bootstrap_admin.py` against an empty database -- consistent
    with a migrated one, rather than two subtly different schemas.

    Idempotent, like everything else in this module: an existing tenant row
    is returned untouched, including a status an operator has since changed
    by hand (this never resets a suspended `"default"` tenant back to
    `"active"`).
    """
    existing = session.scalar(select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID))
    if existing is not None:
        return existing
    tenant = Tenant(id=DEFAULT_TENANT_ID, name="Default", status="active")
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    logger.info("[bootstrap] created default tenant %r", DEFAULT_TENANT_ID)
    return tenant


def seed_rbac(session: Session) -> None:
    """Inserts every permission in `identity.rbac.SEED_PERMISSIONS` and
    every role in `identity.rbac.SEED_ROLES` (plus its `role_permissions`
    grants) that doesn't already exist by name/code.

    Idempotent by design: an existing permission/role is left completely
    untouched (including any grants an operator has since added or removed
    by hand) -- this only ever *adds* what's missing, never resets
    anything back to the seed defaults. Safe to call on every app startup.

    Also calls `ensure_default_tenant` first (Prompt 20). Deliberately
    folded in here rather than left as a second call every caller has to
    remember: this function is already *the* idempotent "make a fresh
    identity database usable" entry point that every caller (
    `scripts/bootstrap_admin.py`, `eval/load/seed_users.py`, and every test
    fixture that builds a `create_all` schema) runs exactly once before
    creating any user -- and creating a user without the default tenant row
    present violates `users.tenant_id`'s `RESTRICT` foreign key on
    PostgreSQL. Splitting it out would have meant a dozen call sites each
    able to forget it, with the failure only showing up in production.
    """
    ensure_default_tenant(session)

    permission_rows: dict[str, PermissionModel] = {
        row.code: row for row in session.scalars(select(PermissionModel)).all()
    }
    for code, description in SEED_PERMISSIONS:
        if code.value in permission_rows:
            continue
        row = PermissionModel(code=code.value, description=description)
        session.add(row)
        session.flush()
        permission_rows[code.value] = row
        logger.info("[identity.bootstrap] seeded permission %r", code.value)

    role_rows: dict[str, Role] = {row.name: row for row in session.scalars(select(Role)).all()}
    for name, description, is_system_role, permissions in SEED_ROLES:
        role = role_rows.get(name)
        if role is None:
            role = Role(name=name, description=description, is_system_role=is_system_role)
            session.add(role)
            session.flush()
            role_rows[name] = role
            logger.info("[identity.bootstrap] seeded role %r", name)

        existing_grant_ids = {
            row.permission_id
            for row in session.scalars(
                select(RolePermission).where(RolePermission.role_id == role.id)
            ).all()
        }
        for permission in permissions:
            permission_row = permission_rows[permission.value]
            if permission_row.id in existing_grant_ids:
                continue
            session.add(RolePermission(role_id=role.id, permission_id=permission_row.id))

    session.commit()


def bootstrap_admin_user(session: Session, settings: Settings) -> User | None:
    """Creates the first admin account from
    `Settings.bootstrap_admin_email`/`bootstrap_admin_password`, if it
    doesn't already exist.

    Returns the created (or already-existing) `User`, or `None` if
    `Settings.bootstrap_admin_enabled` is False (a plain no-op, not an
    error -- see that setting's own docstring for why it's a separate,
    explicit opt-in beyond just having credentials configured).

    Idempotent: if a user with `bootstrap_admin_email` already exists,
    this returns that existing user unchanged (never resets its password,
    never re-assigns roles) rather than raising `DuplicateUserError` --
    re-running this (e.g. every app startup, if
    `bootstrap_admin_enabled` is left on) must never disturb an admin who
    has since changed their own password.

    `seed_rbac` must have already run in the same session/transaction (or
    an earlier one) -- this raises `ValueError` if the `"admin"` role
    doesn't exist yet, since silently creating an adminless "admin" account
    would be a confusing, hard-to-debug outcome.
    """
    if not settings.bootstrap_admin_enabled:
        return None
    # Settings._validate_bootstrap_admin_requires_credentials already
    # guarantees both are set whenever bootstrap_admin_enabled is True.
    assert settings.bootstrap_admin_email is not None
    assert settings.bootstrap_admin_password is not None

    existing = get_user_by_email(session, settings.bootstrap_admin_email)
    if existing is not None:
        logger.info(
            "[identity.bootstrap] admin account %r already exists, skipping.",
            settings.bootstrap_admin_email,
        )
        return existing

    if session.scalar(select(Role).where(Role.name == "admin")) is None:
        raise ValueError(
            "Cannot bootstrap an admin user: the 'admin' role does not exist yet. "
            "Call identity.bootstrap.seed_rbac(session) first."
        )

    user = create_user(
        session,
        email=settings.bootstrap_admin_email,
        password=settings.bootstrap_admin_password.get_secret_value(),
        display_name="Administrator",
        status="active",
        role_name="admin",
    )
    logger.info("[identity.bootstrap] created bootstrap admin account %r.", user.email)
    return user
