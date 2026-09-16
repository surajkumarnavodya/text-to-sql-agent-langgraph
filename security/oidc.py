"""OIDC/JWT validation -- production-grade authentication, alongside (not
replacing) `api/auth.py`'s pre-existing static bearer-token check.

## Why this exists

`api/auth.py`'s `API_AUTH_TOKEN` check (a single shared static secret, no
per-user identity, no expiry, no rotation) is explicitly documented as "a
lightweight hook, not a full auth system." That's a reasonable choice for a
single trusted operator running this locally, but not for an enterprise
deployment with real, distinct human users -- this module is what closes
that gap: standard OIDC ID-token validation against a real identity
provider (Auth0, Okta, Azure AD, Keycloak, or any other OIDC-compliant
issuer), turning "the request presented *a* valid credential" into "the
request was made by a specific, identifiable person, whose token a real
IdP vouches for and that expires on its own."

## Design principles (each defends against a specific, real JWT pitfall)

- **The algorithm allowlist comes from *our* config, never from the
  token.** `jwt.decode(..., algorithms=settings.oidc_algorithms)` is always
  called with an explicit, server-side list -- never derived from the
  token's own `alg` header. This is the standard defense against "alg
  confusion" attacks (e.g. a token claiming `alg: none`, or an RSA
  provider's public key misused as an HMAC shared secret) -- `PyJWKClient`
  only resolves *which key* to use via the token's `kid`, it never decides
  *whether* to trust the token's own algorithm claim.
- **Issuer, audience, expiration, and signature are all verified in the
  same `jwt.decode()` call**, not as separate ad hoc checks a future
  change could accidentally skip one of.
- **JWKS is fetched once and cached** (`PyJWKClient`'s own built-in
  `cache_keys`/`lifespan`), not on every request -- a real per-request
  network round-trip to the identity provider would make every API call
  latency-bound on that provider's availability.
- **No raw token, decoded claims, or signing key material is ever logged.**
  Every failure path logs only a stable, non-sensitive `reason` string via
  `security.audit_log.log_security_event` -- consistent with
  `api/auth.py`'s own static-token-failure logging (2026 Phase 1's MON-02)
  and `security/redaction.py`'s general "logs are a lower-trust sink than
  the request path" principle.
- **Every failure surfaces as the same `TokenValidationError`, with a
  short, generic message** -- callers (`api/auth.py`) turn this into a
  uniform 401, never leaking which specific validation step failed or any
  token-internal detail back to the client (an attacker probing for which
  check to attack next should learn nothing from the response shape).

## What this module does NOT do

It does not manage sessions, issue tokens, handle the OAuth2 authorization-
code exchange, or run a login UI -- this app is a pure OIDC **relying
party**: it only ever validates a bearer token some other, already-deployed
identity provider issued. Token *issuance* is entirely out of scope, by
design (see `docs/AUTHENTICATION.md`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

import httpx
import jwt
from jwt import PyJWKClient

from config.settings import Settings
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT_SECONDS = 5.0


class TokenValidationError(Exception):
    """Raised for any JWT validation failure.

    Always carries a short, non-technical message safe to return directly
    in an HTTP 401 body -- never the underlying `PyJWTError`'s raw text,
    which can echo back header/claim values from the (attacker-controlled)
    token itself.
    """


@dataclass(frozen=True)
class AuthIdentity:
    """The outcome of successfully authenticating one request, regardless
    of which `Settings.auth_mode` produced it.

    `agent/authz.py`'s RBAC layer reads *this*, never a raw token or claims
    dict -- keeping "who is this" (authentication, this module) cleanly
    separated from "what may they do" (authorization, `agent/authz.py`).

    Attributes:
        subject: Stable caller identifier -- the JWT `sub` claim for
            `mode="oidc"`, a fixed sentinel for the other two modes (see
            `api/auth.py`). Used only for audit-trail correlation and as
            the cost-ceiling key (`agent/rate_limit.py`) once real identity
            exists -- never treated as authorization by itself.
        roles: Role names this caller has, lowest-privilege-safe default
            being an *empty* tuple, not an implicit role -- an identity
            with no roles claim gets no roles, not "trusted".
        mode: Which authentication mechanism produced this identity --
            useful for audit logging and for `agent/authz.py` to apply
            mode-specific policy if it ever needs to (e.g. today it does
            not distinguish, but the field exists so that's a policy
            decision, not a missing-data problem).
    """

    subject: str
    roles: tuple[str, ...]
    mode: Literal["none", "static_token", "oidc"]


def extract_roles(claims: Mapping[str, Any], role_claim: str) -> tuple[str, ...]:
    """Reads `role_claim` out of a decoded JWT's claims, tolerating the two
    shapes real providers actually use: a single string (`"role": "admin"`)
    or a list of strings (`"roles": ["admin", "analyst"]`, the more common
    shape -- Auth0/Keycloak/Azure AD custom-claim conventions all vary, but
    converge on "a list of strings" for multi-role users).

    Missing claim, wrong type, or an empty list all resolve to `()` --
    fail-closed (no roles) rather than guessing, consistent with
    `AuthIdentity.roles`'s own "no implicit role" contract.
    """
    value = claims.get(role_claim)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if isinstance(item, str) and item)
    return ()


def _discover_jwks_url(issuer: str) -> str:
    """Standard OIDC discovery: `GET <issuer>/.well-known/openid-configuration`,
    read its `jwks_uri` field. `issuer` is operator-configured (`.env`),
    never request-controlled, so this is not an SSRF-relevant fetch the way
    `media_gen/download.py`'s provider-URL fetch is."""
    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = httpx.get(discovery_url, timeout=_DISCOVERY_TIMEOUT_SECONDS)
        response.raise_for_status()
        jwks_uri = response.json()["jwks_uri"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise TokenValidationError("Unable to reach the identity provider.") from exc
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise TokenValidationError("Unable to reach the identity provider.")
    return jwks_uri


@lru_cache(maxsize=1)
def _cached_jwks_url(issuer: str) -> str:
    """Process-lifetime cache of the discovered JWKS URL, keyed on
    `issuer` -- same `lru_cache`-singleton pattern this codebase already
    uses for other expensive-to-repeat, effectively-static lookups (e.g.
    `db.connection._cached_engine`). A config change requires a process
    restart to take effect, same tradeoff as every other such cache here.
    """
    return _discover_jwks_url(issuer)


@lru_cache(maxsize=8)
def _get_jwk_client(jwks_url: str) -> PyJWKClient:
    """Process-lifetime `PyJWKClient` per JWKS URL -- handles its own
    fetch/cache/key-rotation-by-`kid` internally (`cache_keys=True`,
    `lifespan=600`: re-fetch the key set at most once per 10 minutes, plus
    an automatic one-shot re-fetch on a `kid` miss to tolerate the
    identity provider rotating its signing key between our cache refreshes).
    `maxsize=8` is generous headroom for multi-issuer setups without being
    unbounded -- this app realistically configures exactly one issuer.
    """
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=600)


def _log_rejection(reason: str) -> None:
    log_security_event(
        "oidc_token_rejected",
        "warning",
        "A JWT was rejected during OIDC authentication.",
        reason=reason,
    )


def validate_token(token: str, settings: Settings) -> AuthIdentity:
    """Validates `token` as an OIDC ID/access token per `settings`'s
    `oidc_*` fields, returning the caller's `AuthIdentity` on success.

    Verifies, in one `jwt.decode()` call: signature (against the
    provider's real public key, resolved via JWKS + the token's own `kid`),
    issuer, audience, and expiration/not-before (with
    `oidc_clock_skew_seconds` leeway). Never trusts the token's own `alg`
    header (see this module's docstring).

    Raises:
        TokenValidationError: on any validation failure, or if OIDC isn't
            configured at all (`settings.oidc_issuer is None`) -- a
            programming-error case for a caller that should have checked
            `settings.auth_mode == "oidc"` first, still handled safely
            rather than raising a confusing lower-level exception.
    """
    if settings.oidc_issuer is None:
        raise TokenValidationError("OIDC authentication is not configured.")

    jwks_url = settings.oidc_jwks_url or _cached_jwks_url(settings.oidc_issuer)

    try:
        jwk_client = _get_jwk_client(jwks_url)
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(settings.oidc_algorithms),
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            leeway=settings.oidc_clock_skew_seconds,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        _log_rejection("expired")
        raise TokenValidationError("Token has expired.") from exc
    except jwt.ImmatureSignatureError as exc:
        _log_rejection("not_yet_valid")
        raise TokenValidationError("Token is not yet valid.") from exc
    except jwt.InvalidIssuerError as exc:
        _log_rejection("invalid_issuer")
        raise TokenValidationError("Token issuer does not match.") from exc
    except jwt.InvalidAudienceError as exc:
        _log_rejection("invalid_audience")
        raise TokenValidationError("Token audience does not match.") from exc
    except jwt.MissingRequiredClaimError as exc:
        _log_rejection("missing_required_claim")
        raise TokenValidationError("Token is missing a required claim.") from exc
    except jwt.InvalidSignatureError as exc:
        _log_rejection("invalid_signature")
        raise TokenValidationError("Token signature is invalid.") from exc
    except jwt.PyJWKClientError as exc:
        _log_rejection("jwks_lookup_failed")
        raise TokenValidationError("Unable to verify the token's signing key.") from exc
    except jwt.PyJWTError as exc:
        _log_rejection("invalid_token")
        raise TokenValidationError("Token is invalid.") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        _log_rejection("missing_subject")
        raise TokenValidationError("Token is missing a subject claim.")

    roles = extract_roles(claims, settings.oidc_role_claim)
    return AuthIdentity(subject=subject, roles=roles, mode="oidc")
