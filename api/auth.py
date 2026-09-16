"""Authentication for `api/` endpoints -- dispatches across the three
`Settings.auth_mode` values (see that property's own docstring):

- **"none"** (default, dev-only): no credential required at all. Unchanged
  from before this module grew OIDC support -- a fresh clone with no
  `.env` auth config behaves exactly as it always did. `Settings`'s own
  `_require_identity_in_production` validator refuses to let a deployment
  reach this mode while also declaring `ENVIRONMENT=production`.
- **"static_token"**: the original `API_AUTH_TOKEN` shared-secret check,
  unchanged in mechanism (timing-safe comparison) and unchanged in what it
  grants -- a single shared credential with no per-user identity to scope
  down, so a valid token is treated as full admin access, same as it
  implicitly always was before role checks (`agent/authz.py`) existed.
- **"oidc"**: real per-user identity via `security/oidc.py`'s JWT
  validation -- the production-grade option, see `docs/AUTHENTICATION.md`.
  Can be configured *alongside* a static token (interactive users via
  OIDC, service/CI callers via the static token, both accepted) --
  `verify_api_key` tries OIDC first when an OIDC-shaped bearer token is
  presented, and falls back to the static-token check.

Every successful authentication (including the "none" no-op) attaches a
`security.oidc.AuthIdentity` to `request.state.auth_identity` --
`agent/authz.py`'s RBAC dependencies read *that*, never re-parsing a token
or re-checking `auth_mode` themselves, keeping authentication and
authorization cleanly separated.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from config.settings import get_settings
from security.audit_log import log_security_event
from security.oidc import AuthIdentity, TokenValidationError, validate_token

_BEARER_PREFIX = "Bearer "

# The "none" mode's identity -- see this module's docstring for why it
# carries the highest role: preserving today's actual behavior (anyone who
# can reach the API already has full access when no auth is configured at
# all) rather than silently starting to reject requests once `agent/authz.py`
# lands role checks on top of this module.
_DEV_MODE_IDENTITY = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")

# What a valid static shared-secret grants -- see this module's docstring
# for why "admin" (no per-user identity to scope down further).
_STATIC_TOKEN_IDENTITY = AuthIdentity(subject="static-token", roles=("admin",), mode="static_token")


def _is_matching_bearer_token(authorization_header: str, expected_token: str) -> bool:
    if not authorization_header.startswith(_BEARER_PREFIX):
        return False
    provided = authorization_header[len(_BEARER_PREFIX) :]
    # Constant-time comparison -- a naive `==` would leak how many leading
    # characters matched via response-timing differences, a real (if minor)
    # side channel for guessing the configured token.
    return hmac.compare_digest(provided, expected_token)


def _log_auth_failed(request: Request, reason: str) -> None:
    log_security_event(
        "auth_failed",
        "warning",
        "A request was rejected at the API authentication gate.",
        reason=reason,
        client_ip=request.client.host if request.client else "unknown",
        path=request.url.path,
    )


def verify_api_key(request: Request, authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: authenticates the request per `Settings.auth_mode`,
    raising `HTTPException(401)` on failure and otherwise attaching a
    `security.oidc.AuthIdentity` to `request.state.auth_identity`.

    2026 Phase 1 security review (finding MON-02): every rejection is
    logged to the structured audit trail (`security.audit_log`) -- only the
    failure *reason* and client IP, never the attempted token/credential
    value itself, which would turn the audit log into its own
    credential-leakage surface (see `security/oidc.py`'s own docstring for
    the same principle applied to JWT claims).
    """
    settings = get_settings()

    if settings.auth_mode == "none":
        request.state.auth_identity = _DEV_MODE_IDENTITY
        return

    if authorization is None:
        _log_auth_failed(request, "missing_header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.auth_mode == "oidc":
        token = (
            authorization[len(_BEARER_PREFIX) :]
            if authorization.startswith(_BEARER_PREFIX)
            else None
        )
        if token is not None:
            try:
                identity = validate_token(token, settings)
            except TokenValidationError:
                identity = None
            if identity is not None:
                request.state.auth_identity = identity
                return
        # Falls through to the static-token check below (if configured) --
        # lets a service/CI caller use API_AUTH_TOKEN even while OIDC is
        # the primary mode for interactive users. If no static token is
        # configured, this loop is a no-op and the rejection below fires.

    if settings.api_auth_token is not None:
        expected = settings.api_auth_token.get_secret_value()
        if _is_matching_bearer_token(authorization, expected):
            request.state.auth_identity = _STATIC_TOKEN_IDENTITY
            return

    _log_auth_failed(
        request, "invalid_token" if settings.auth_mode == "static_token" else "invalid_credential"
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid Authorization header.",
        headers={"WWW-Authenticate": "Bearer"},
    )
