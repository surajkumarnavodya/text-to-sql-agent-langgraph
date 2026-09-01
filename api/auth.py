"""Optional bearer-token auth for `api/` endpoints.

Deliberately lightweight -- see `config.settings.Settings.api_auth_token`'s
docstring and `docs/DEPLOYMENT.md`. This is a hook for "at least require a
shared secret," not a real multi-user auth system: no per-user identity, no
token issuance/rotation, no sessions. It no-ops entirely (allows every
request) when `API_AUTH_TOKEN` is unset, which is the default -- consistent
with this project's existing single-tenant, local-first design (see
SECURITY.md). Anything beyond local/trusted-network use should sit behind a
real authenticating reverse proxy regardless of whether this is set.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from config.settings import get_settings

_BEARER_PREFIX = "Bearer "


def _is_matching_bearer_token(authorization_header: str, expected_token: str) -> bool:
    if not authorization_header.startswith(_BEARER_PREFIX):
        return False
    provided = authorization_header[len(_BEARER_PREFIX) :]
    # Constant-time comparison -- a naive `==` would leak how many leading
    # characters matched via response-timing differences, a real (if minor)
    # side channel for guessing the configured token.
    return hmac.compare_digest(provided, expected_token)


def verify_api_key(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401s if `API_AUTH_TOKEN` is set and the request's
    `Authorization` header doesn't present it as a matching bearer token.

    A no-op when `API_AUTH_TOKEN` is unset (the default).
    """
    settings = get_settings()
    if settings.api_auth_token is None:
        return
    expected = str(settings.api_auth_token)
    if authorization is None or not _is_matching_bearer_token(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
