"""Authentication for `api/` endpoints -- dispatches across four possible
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
  validation against an *external* identity provider -- see
  `docs/AUTHENTICATION.md`.
- **"local"**: this app's own self-hosted accounts (`identity/`,
  `api/identity_auth.py`) -- a JWT this process itself issued
  (`identity.security.create_access_token`), validated against
  `Settings.jwt_secret_key`/`jwt_private_key_path`, never an external
  provider's key.

All four can be configured *simultaneously* -- `verify_api_key` tries
local first (a request bearing a locally-issued token never wastes an
attempt against OIDC's key/algorithm, and vice versa -- see
`identity.security.looks_like_local_token`), then OIDC, then falls back to
the static-token check, matching this module's pre-existing "OIDC + static
token can coexist" posture, just extended by one more link in the chain.

Every successful authentication (including the "none" no-op) attaches a
`security.oidc.AuthIdentity` to `request.state.auth_identity` --
`agent/authz.py`'s RBAC dependencies read *that*, never re-parsing a token
or re-checking `auth_mode` themselves, keeping authentication and
authorization cleanly separated.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status
from identity.exceptions import LocalTokenValidationError
from identity.security import looks_like_local_token, validate_local_token

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

    if settings.local_auth_enabled:
        token = (
            authorization[len(_BEARER_PREFIX) :]
            if authorization.startswith(_BEARER_PREFIX)
            else None
        )
        # looks_like_local_token checks the (unverified) `iss` claim before
        # attempting a real, signature-verifying decode -- so a request
        # bearing an externally-issued OIDC token, or the static token
        # (not a JWT shape at all), never wastes a doomed validation
        # attempt against this app's own signing key/algorithm; see that
        # function's own docstring.
        if token is not None and looks_like_local_token(token, settings):
            try:
                claims = validate_local_token(token, settings)
            except LocalTokenValidationError:
                claims = None
            if claims is not None:
                request.state.auth_identity = AuthIdentity(
                    subject=claims.subject, roles=claims.roles, mode="local"
                )
                return
        # Falls through to OIDC/static-token below -- a token that isn't
        # locally issued (or fails local validation) may still be valid
        # under one of those, exactly like OIDC's own fall-through to the
        # static token already works.

    if settings.oidc_issuer is not None:
        # Checked directly against `oidc_issuer` (not `settings.auth_mode`)
        # -- `auth_mode` now reports "local" whenever local auth is also
        # enabled (see that property's own docstring: it summarizes the
        # *highest-priority* mechanism, not "the only one"), and OIDC must
        # still be attempted here even when local auth is configured
        # alongside it.
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
