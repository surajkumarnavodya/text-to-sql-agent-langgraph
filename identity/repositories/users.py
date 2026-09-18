"""User accounts, roles, and the granular permission lookup
(`identity/rbac.py`'s DB-backed side).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from identity.exceptions import AccountLockedError, DuplicateUserError
from identity.models import Permission as PermissionModel
from identity.models import Role, RolePermission, User, UserRole
from identity.rbac import DEFAULT_ROLE_NAME
from identity.security import hash_password


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
    password: str,
    display_name: str | None = None,
    username: str | None = None,
    status: str = "active",
    role_name: str = DEFAULT_ROLE_NAME,
) -> User:
    """Creates a new user with a hashed password and the given default role
    assigned. `status` is the caller's responsibility to set correctly
    (`api/identity_auth.py`'s registration route passes
    `"pending_verification"` when `Settings.require_email_verification` is
    on, `"active"` otherwise; `api/identity_admin.py`'s admin-create route
    can pass either directly).

    Raises:
        DuplicateUserError: `email` (normalized) already exists.
    """
    normalized_email = normalize_email(email)
    if get_user_by_email(session, normalized_email) is not None:
        raise DuplicateUserError(f"A user with email {normalized_email!r} already exists.")

    user = User(
        email=normalized_email,
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        status=status,
        password_changed_at=datetime.now(UTC),
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


def update_display_name(session: Session, user: User, display_name: str) -> User:
    """Sets/changes `user.display_name` -- the already-validated value
    (`identity.display_name.validate_display_name`, applied by
    `identity.schemas.UpdateProfileRequest`'s own field validator before
    this function is ever called) is trusted as-is here."""
    user.display_name = display_name
    session.commit()
    session.refresh(user)
    return user
