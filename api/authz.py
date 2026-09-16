"""FastAPI dependency wiring for `agent.authz`'s RBAC policy.

Kept separate from `agent/authz.py` itself so that module stays free of any
web-framework dependency (it's also called directly from `agent/nodes.py`/
`agent/orchestrator/nodes.py`, business logic that has no business knowing
about `Request`/`HTTPException`) -- see that module's own docstring.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from agent.authz import Permission, has_permission
from api.auth import verify_api_key
from security.audit_log import log_security_event
from security.oidc import AuthIdentity


def get_auth_identity(request: Request) -> AuthIdentity:
    """Reads the `AuthIdentity` `api.auth.verify_api_key` attached to
    `request.state` -- the one place any route should ever pull "who is
    the caller" from once authentication has already run. Raises a 500
    (not a 401) if it's missing, since that only happens from a genuine
    wiring bug (a route using this without `verify_api_key` having run
    first), never from anything a caller controls.
    """
    identity = getattr(request.state, "auth_identity", None)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication did not run before this authorization check.",
        )
    return identity


def require_permission(permission: Permission):
    """FastAPI dependency factory: 403s unless the authenticated caller has
    `permission`. Self-sufficient -- depends on `verify_api_key` itself
    (via `Depends`), so a route needs only
    `Depends(require_permission(Permission.X))` and nothing else; it does
    not also need a separate `Depends(verify_api_key)` (FastAPI caches a
    dependency's result per request, so `verify_api_key` still only runs
    once even where a router also lists it at the router level).

    Every denial is audit-logged (`authz_denied`) with the caller's
    subject, roles, and the permission that was missing -- unlike an
    authentication failure, there is no credential-guessing risk in
    logging this detail (roles/permission names aren't secrets), and it is
    exactly the record needed to investigate a privilege-escalation
    attempt after the fact.
    """

    def _dependency(request: Request, _auth: None = Depends(verify_api_key)) -> AuthIdentity:
        identity = get_auth_identity(request)
        if not has_permission(identity, permission):
            log_security_event(
                "authz_denied",
                "warning",
                "A request was denied at the authorization gate.",
                subject=identity.subject,
                roles=list(identity.roles),
                required_permission=permission.value,
                path=request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return identity

    return _dependency
