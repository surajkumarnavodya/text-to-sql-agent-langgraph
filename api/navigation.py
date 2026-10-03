"""Server-side navigation endpoints -- Prompt 32 (`32_ROLE_BASED_NAVIGATION_CONTRACT.md`).

- `GET /navigation`: the screens and actions the caller may use. Computed
  here, from the same permission checks the real routes make, so the frontend
  never has to guess from a role name.
- `POST /navigation/access-denied`: the frontend reports that it refused to
  show a screen to a caller. The report is audit-logged, but only for a real
  screen path, and it is rate-limited like every other write.

Authentication runs first (`verify_api_key`). A local-account caller's
identity permissions come from the database, through the same
`require_local_user` every identity route uses, so a suspended tenant or a
deleted account loses navigation on its next request, exactly as it loses
data access. Any other caller gets only the permissions its roles carry in
`agent.authz`, which is also all the identity routes would let it reach.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from identity.db import get_identity_session
from identity.repositories.users import get_user_permissions
from sqlalchemy.orm import Session

from agent.authz import permissions_for
from api.auth import verify_api_key
from api.authz import get_auth_identity
from api.identity_authz import require_local_auth_enabled, require_local_user
from api.navigation_schemas import AccessDeniedReport, NavigationOut, NavItemOut
from api.rate_limit import enforce_api_action_rate_limit
from config.settings import get_settings
from security.audit_log import log_security_event
from security.navigation import nav_item_for_path, resolve_navigation
from security.oidc import AuthIdentity
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/navigation", tags=["navigation"])


def _identity_permissions_for(
    request: Request, identity: AuthIdentity
) -> tuple[frozenset[str], str | None]:
    """The caller's DB-backed identity permissions and resolved tenant.

    Only a local account has DB-backed permissions. For any other caller this
    returns an empty set, which matches `require_local_user`, the gate those
    routes sit behind. A local account that fails the tenant check raises the
    same 401 it would on any identity route.
    """
    if identity.mode != "local":
        return frozenset(), None
    settings = require_local_auth_enabled()
    session: Session = get_identity_session(settings)
    try:
        user, _ = require_local_user(request, session, None)
        return get_user_permissions(session, user.id), resolve_actor_tenant_id(user)
    finally:
        session.close()


@router.get("", response_model=NavigationOut)
def get_navigation(
    request: Request,
    _auth: None = Depends(verify_api_key),
    identity: AuthIdentity = Depends(get_auth_identity),
) -> NavigationOut:
    agent_permissions = permissions_for(identity.roles)
    identity_permissions, tenant_id = _identity_permissions_for(request, identity)
    items, capabilities = resolve_navigation(
        agent_permissions=agent_permissions,
        identity_permissions=identity_permissions,
    )
    return NavigationOut(
        items=[NavItemOut(id=item.id, path=item.path, group=item.group) for item in items],
        capabilities=capabilities,
        roles=list(identity.roles),
        tenant_id=tenant_id,
    )


@router.post("/access-denied", status_code=status.HTTP_204_NO_CONTENT)
def report_access_denied(
    payload: AccessDeniedReport,
    request: Request,
    _auth: None = Depends(verify_api_key),
    identity: AuthIdentity = Depends(get_auth_identity),
) -> Response:
    """Audits a blocked screen. Always answers 204, whether or not the path
    was logged, so a caller cannot use the response to probe which paths
    exist."""
    enforce_api_action_rate_limit(request, "navigation_access_denied", get_settings(), identity)
    item = nav_item_for_path(payload.path)
    if item is not None:
        log_security_event(
            "ui_route_denied",
            "warning",
            "A signed-in caller opened a screen they are not permitted to use.",
            screen=item.id,
            path=item.path,
            subject=identity.subject,
            roles=list(identity.roles),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
