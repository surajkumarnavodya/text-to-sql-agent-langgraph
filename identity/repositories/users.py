"""User accounts, roles, and the granular permission lookup
(`identity/rbac.py`'s DB-backed side).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from identity.exceptions import AccountLockedError, DuplicateUserError
from identity.models import Permission as PermissionModel
from identity.models import Role, RolePermission, User, UserRole
from identity.rbac import DEFAULT_ROLE_NAME
from identity.security import hash_password
from security.tenancy import DEFAULT_TENANT_ID


def _aware_utc(value: datetime | None) -> datetime | None:
    """Normalizes a datetime read back from the database to timezone-aware
    UTC. PostgreSQL's `TIMESTAMPTZ` (what every `DateTime(timezone=True)`
    column here maps to in production) round-trips a timezone-aware
    Python `datetime` correctly on its own; SQLite (this codebase's own
    testing-only engine, see `identity/models.py`'s module docstring)
    does not preserve timezone info at all, always returning a naive
    datetime on read -- comparing that directly against
    `datetime.now(timezone.utc)` raises `TypeError`. Assuming UTC for a
    naive value is correct here specifically because every write path in
    this module always constructs the value from `datetime.now(timezone
    .utc)` in the first place; this is purely a read-back normalization,
    not a guess about what timezone the data is "really" in.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def normalize_email(email: str) -> str:
    """Lowercases + strips an email for storage/lookup -- so
    `Foo@Example.com` and `foo@example.com` are treated as the same
    account, matching how every real mail provider treats the domain part
    (and, in practice, almost always the local part too)."""
    return email.strip().lower()


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == normalize_email(email)))


def get_user_by_id(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def create_user(
    session: Session,
    *,
    email: str,
    password: str | None = None,
    display_name: str | None = None,
    username: str | None = None,
    status: str = "active",
    role_name: str = DEFAULT_ROLE_NAME,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> User:
    """Creates a new user with a hashed password and the given default role
    assigned. `status` is the caller's responsibility to set correctly
    (`api/identity_auth.py`'s registration route passes
    `"pending_verification"` when `Settings.require_email_verification` is
    on, `"active"` otherwise; `api/identity_admin.py`'s admin-create route
    can pass either directly).

    `password=None` (2026-09-28, Google sign-in) creates an account with
    `password_hash=None` -- no local password at all, not a blank/empty
    one. Every *local* login path (`api/identity_auth.py::login`) must
    check for this explicitly before calling `identity.security
    .verify_password`, since that function is never meant to be called
    with a `None` hash. Callers creating a Google-only account should use
    `identity.repositories.external_identities.create_user_from_external_identity`
    rather than calling this directly, so the identity-link row is created
    in the same transaction.

    `tenant_id` (Prompt 20) is which tenant the new account belongs to --
    the single server-side fact every tenant check in this codebase
    subsequently resolves from (`security.tenancy.resolve_actor_tenant_id`).
    Defaults to `security.tenancy.DEFAULT_TENANT_ID`, so every existing
    caller creates an account in exactly the tenant it implicitly created
    one in before this parameter existed. **No request body, query parameter
    or header anywhere in `api/` reaches it**: all four real call sites
    (self-registration in `api/identity_auth.py`, the first-admin bootstrap
    in `identity/bootstrap.py`, Google sign-up in
    `identity/repositories/external_identities.py`, and
    `eval/load/seed_users.py`) leave it at the default, so a non-default
    tenant can only ever be assigned by an operator running code directly
    against the database -- there is no self-service tenant selection to
    abuse. Note the `RESTRICT` foreign key onto `tenants.id`: an unknown
    tenant id fails the insert on PostgreSQL rather than silently creating
    an orphaned account.

    Raises:
        DuplicateUserError: `email` (normalized) already exists.
    """
    normalized_email = normalize_email(email)
    if get_user_by_email(session, normalized_email) is not None:
        raise DuplicateUserError(f"A user with email {normalized_email!r} already exists.")

    user = User(
        tenant_id=tenant_id,
        email=normalized_email,
        username=username,
        display_name=display_name,
        password_hash=hash_password(password) if password is not None else None,
        status=status,
        password_changed_at=datetime.now(UTC) if password is not None else None,
    )
    session.add(user)
    session.flush()  # assigns user.id before the role-assignment insert below

    role = session.scalar(select(Role).where(Role.name == role_name))
    if role is not None:
        # Silently skips assigning a role that doesn't exist in the
        # `roles` table yet, rather than raising -- a fresh identity
        # database not yet seeded (`identity/bootstrap.py`) shouldn't make
        # user creation itself fail; the account still exists, just with
        # no roles (and therefore no permissions at all, fail-closed,
        # until an admin assigns one).
        session.add(UserRole(user_id=user.id, role_id=role.id))

    session.commit()
    session.refresh(user)
    return user


def get_user_roles(session: Session, user_id: uuid.UUID) -> tuple[str, ...]:
    rows = session.scalars(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).all()
    return tuple(rows)


def get_user_permissions(session: Session, user_id: uuid.UUID) -> frozenset[str]:
    """The caller's full, DB-backed granular permission set (`identity.rbac
    .Permission` codes, as plain strings) -- a fresh join every call,
    deliberately not cached, so a permission/role change takes effect on
    the caller's very next request rather than only after their access
    token expires (see `identity.security.create_access_token`'s own
    docstring for why permissions aren't embedded in the token itself).
    """
    rows = session.scalars(
        select(PermissionModel.code)
        .join(RolePermission, RolePermission.permission_id == PermissionModel.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
    ).all()
    return frozenset(rows)


def assign_role(
    session: Session, *, user_id: uuid.UUID, role_name: str, assigned_by_user_id: uuid.UUID | None
) -> None:
    """Idempotent: assigning a role the user already has is a no-op, not a
    duplicate-key error.

    Raises:
        ValueError: `role_name` doesn't exist in the `roles` table.
    """
    role = session.scalar(select(Role).where(Role.name == role_name))
    if role is None:
        raise ValueError(f"Role {role_name!r} does not exist.")
    existing = session.get(UserRole, {"user_id": user_id, "role_id": role.id})
    if existing is not None:
        return
    session.add(UserRole(user_id=user_id, role_id=role.id, assigned_by_user_id=assigned_by_user_id))
    session.commit()


def remove_role(session: Session, *, user_id: uuid.UUID, role_name: str) -> None:
    role = session.scalar(select(Role).where(Role.name == role_name))
    if role is None:
        return
    existing = session.get(UserRole, {"user_id": user_id, "role_id": role.id})
    if existing is not None:
        session.delete(existing)
        session.commit()


def record_login_success(session: Session, user: User) -> None:
    user.last_login_at = datetime.now(UTC)
    user.failed_login_count = 0
    # identity/models.py's own module docstring explains why this column is
    # annotated `Mapped[datetime]`, not `Mapped[datetime | None]`, despite
    # being genuinely nullable (a Python 3.14 + SQLAlchemy 2.0.36
    # incompatibility) -- mypy sees the attribute as non-Optional as a
    # result, which this assignment correctly contradicts at runtime.
    user.locked_until = None  # type: ignore[assignment]
    session.commit()


def check_not_locked(user: User) -> None:
    """Raises `AccountLockedError` if `user.locked_until` is still in the
    future. Callers check this *before* verifying the password (see
    `api/identity_auth.py`'s login route) -- a locked account should reject
    every attempt uniformly during its cooldown, not leak "the password
    would have been right" via a different response shape.
    """
    locked_until = _aware_utc(user.locked_until)
    if locked_until is not None and locked_until > datetime.now(UTC):
        raise AccountLockedError(
            f"Account {user.id} is locked until {user.locked_until.isoformat()}."
        )


def record_login_failure(
    session: Session, user: User, *, max_attempts: int, lockout_minutes: int
) -> bool:
    """Increments `failed_login_count`; locks the account
    (`locked_until = now + lockout_minutes`) once `max_attempts` is
    reached. Returns True if this call just triggered a new lock (so the
    caller can log a distinct `account_locked` sign-in event), False
    otherwise.
    """
    user.failed_login_count += 1
    just_locked = False
    if user.failed_login_count >= max_attempts:
        user.locked_until = datetime.now(UTC) + timedelta(minutes=lockout_minutes)
        just_locked = True
    session.commit()
    return just_locked


def change_password(session: Session, user: User, new_password: str) -> None:
    user.password_hash = hash_password(new_password)
    user.password_changed_at = datetime.now(UTC)
    session.commit()


def count_users_by_tenant(session: Session) -> dict[str, int]:
    """`{tenant_id: active_account_count}` across every tenant -- one
    grouped query, not N `list_users` calls, feeding Prompt 28's platform-
    admin tenants listing."""
    rows = session.execute(
        select(User.tenant_id, func.count())
        .where(User.deleted_at.is_(None))
        .group_by(User.tenant_id)
    ).all()
    return {tenant_id: count for tenant_id, count in rows}


def list_users(
    session: Session,
    *,
    tenant_id: str | None = None,
    role_name: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[User]:
    """Lists accounts, newest first -- Prompt 28
    (`28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`), the platform-admin
    dashboard's own Users section.

    `tenant_id=None` (the default) lists **across every tenant** -- unlike
    every other tenant-aware query in this codebase, which filters to one
    tenant by default. This is intentional: the only caller of this
    function is `api/platform_admin.py`, already gated on
    `identity.rbac.Permission.PLATFORM_ADMIN`, and that permission's
    entire purpose is cross-tenant visibility (see that permission's own
    docstring). A route for an *ordinary* tenant-scoped admin would need
    to pass its own `tenant_id` explicitly -- this function does not
    default to "safe," it defaults to "complete," because its one caller
    already proved it may see everything.

    `limit` bounds a single page (no cursor/offset pagination yet -- a
    disclosed, narrower scope than a production user-management screen
    would eventually want for a very large user base).
    """
    statement = select(User).where(User.deleted_at.is_(None))
    if tenant_id is not None:
        statement = statement.where(User.tenant_id == tenant_id)
    if status is not None:
        statement = statement.where(User.status == status)
    if role_name is not None:
        statement = (
            statement.join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.name == role_name)
        )
    statement = statement.order_by(User.created_at.desc()).limit(limit)
    return list(session.scalars(statement))


class RoleWithPermissions:
    """A plain, ORM-independent read shape -- `role` plus its granted
    `permissions`, exactly what `GET /platform-admin/roles` needs and
    nothing a response schema would have to strip back out."""

    __slots__ = ("role", "permissions")

    def __init__(self, role: Role, permissions: tuple[str, ...]) -> None:
        self.role = role
        self.permissions = permissions


def list_roles_with_permissions(session: Session) -> list[RoleWithPermissions]:
    """Every role, each with its granted permission codes -- Prompt 28's
    Roles & Permissions dashboard section. Read-only: this codebase has
    no route that edits a role's own permission grants (only which roles
    a *user* holds, via `assign_role`/`remove_role` below), matching
    `identity/rbac.py`'s own docstring that `SEED_ROLES` is initial data
    an operator may extend by hand, not something this app's UI manages.
    """
    roles = list(session.scalars(select(Role).order_by(Role.name)))
    results = []
    for role in roles:
        codes = session.scalars(
            select(PermissionModel.code)
            .join(RolePermission, RolePermission.permission_id == PermissionModel.id)
            .where(RolePermission.role_id == role.id)
            .order_by(PermissionModel.code)
        ).all()
        results.append(RoleWithPermissions(role=role, permissions=tuple(codes)))
    return results


def update_display_name(session: Session, user: User, display_name: str) -> User:
    """Sets/changes `user.display_name` -- the already-validated value
    (`identity.display_name.validate_display_name`, applied by
    `identity.schemas.UpdateProfileRequest`'s own field validator before
    this function is ever called) is trusted as-is here."""
    user.display_name = display_name
    session.commit()
    session.refresh(user)
    return user
