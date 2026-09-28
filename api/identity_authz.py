"""FastAPI dependency wiring for the identity module's own auth checks --
mirrors `api/authz.py`'s shape (request-scoped wiring layered on top of
`api.auth.verify_api_key`), kept in its own module for the same reason
`api/authz.py` is separate from `agent/authz.py`: this file may import
FastAPI/SQLAlchemy freely, while `identity/rbac.py` stays free of both.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from identity.db import get_identity_session
from identity.models import User
from identity.rbac import Permission as IdentityPermission
from identity.repositories.users import get_user_by_id, get_user_permissions
from sqlalchemy.orm import Session

from api.auth import verify_api_key
from api.authz import get_auth_identity
from config.settings import Settings, get_settings
from security.audit_log import log_security_event


def require_local_auth_enabled(settings: Settings | None = None) -> Settings:
    """404s -- not 503 -- when local auth is off. Unlike a genuinely
    *misconfigured* optional feature (e.g. `RagStoreNotConfiguredError`'s
    503, which tells an operator "you turned this on but forgot a step"),
    local auth being off is this feature's own default, common state, no
    different from any other route this app simply doesn't define -- a
    404 is the more honest status for "this doesn't exist here," and
    avoids revealing to an unauthenticated prober that a whole auth
    subsystem is present-but-disabled versus genuinely absent.

    The single shared gate for every `/auth/*` route (both this module's
    own `get_identity_db`/`require_local_user` dependencies and
    `api/identity_auth.py`'s handlers import this rather than each
    re-implementing the check) -- critically, `get_identity_db` below
    calls this *before* ever attempting to open a session, since a FastAPI
    dependency runs before the route body, so a route-body-only check
    would never run in time to prevent `get_identity_session` from raising
    an unhandled `IdentityNotConfiguredError` (a 500) the moment local
    auth is off.
    """
    settings = settings or get_settings()
    if not settings.local_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return settings


def get_identity_db() -> Iterator[Session]:
    """Yields a `Session` bound to the identity database, closed once the
    request finishes -- the FastAPI dependency-with-`yield` equivalent of
    every other DB-backed module's own `with engine.connect() as conn:`
    context-manager convention."""
    settings = require_local_auth_enabled()
    session = get_identity_session(settings)
    try:
        yield session
    finally:
        session.close()


def require_local_user(
    request: Request,
    session: Session = Depends(get_identity_db),
    _auth: None = Depends(verify_api_key),
) -> tuple[User, Session]:
    """403s unless the caller authenticated via a locally-issued token
    (`AuthIdentity.mode == "local"`, see `api/auth.py`), then loads and
    returns the matching `identity.models.User` row alongside the open
    `Session` it was loaded through (so a route handler can keep using the
    same session for further reads/writes without opening a second one).

    A caller authenticated via OIDC/the static token/no-auth-mode gets a
    403 here, not a 401 -- they *are* authenticated, just not as a local
    account, so this is an authorization distinction (this action requires
    a local account) rather than an authentication failure.
    """
    identity = get_auth_identity(request)
    if identity.mode != "local":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires signing in with a local account.",
        )
    try:
        user_id = uuid.UUID(identity.subject)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session."
        ) from exc

    user = get_user_by_id(session, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account no longer exists."
        )
    return user, session


def require_identity_permission(permission: IdentityPermission):
    """FastAPI dependency factory: 403s unless the local caller's DB-backed
    `identity.rbac.Permission` grants include `permission`.

    `identity.rbac`'s granular permission codes (`SEED_ROLES`/
    `SEED_PERMISSIONS`, seeded by `identity.bootstrap.seed_rbac`) existed
    in this codebase's schema and repository layer
    (`identity.repositories.users.get_user_permissions`) before this
    function did, but nothing had actually wired them into a route yet --
    every existing `identity/`-backed endpoint (`api/chat_history.py`) only
    ever checked "is this a valid local account," relying on per-resource
    `user_id` ownership scoping alone, never this table. This is the first
    call site, added for conversation sharing's own explicit RBAC
    requirement -- it does not change behavior for any pre-existing route,
    and every default seeded role already grants every `shares.*`
    permission (see `identity/rbac.py`'s own `_VIEWER`/`_OWN_RESOURCE_PERMISSIONS`
    sets), so this is additive enforcement, not a new restriction for an
    existing user.

    Mirrors `api.authz.require_permission`'s exact shape (same denial
    logging, same self-sufficient `Depends(require_local_user)` composition)
    for the sibling, more-granular permission system.
    """

    def _dependency(
        request: Request, user_and_session: tuple[User, Session] = Depends(require_local_user)
    ) -> tuple[User, Session]:
        user, session = user_and_session
        granted = get_user_permissions(session, user.id)
        if permission.value not in granted:
            log_security_event(
                "identity_authz_denied",
                "warning",
                "A request was denied at the identity-permission gate.",
                user_id=str(user.id),
                required_permission=permission.value,
                path=request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return user, session

    return _dependency
