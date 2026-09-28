"""Server-side verification of a Google ID token -- the trust boundary for
"Continue with Google" (`POST /auth/google`, `identity/repositories
/external_identities.py`).

## Why the ID-token-credential flow, not the authorization-code flow

Two Google-maintained sign-in flows were considered (see the task's own
framing): a backend authorization-code exchange (needs a client *secret*,
a redirect endpoint, and a `code`/`state`/PKCE round trip), or Google
Identity Services' **Sign In With Google button**, whose JS callback hands
the page a signed **ID token** (`credential`) directly. This app uses the
second one, deliberately:

- **No client secret is ever needed or stored.** Verifying an ID token
  only requires the *public* client ID (to check the `aud` claim) -- there
  is no code to exchange, so there is nothing secret to hold server-side
  at all. This directly serves the task's own "prevent client secrets
  entering frontend bundles" goal by the simplest possible means: not
  having one.
- **This app never needs Google access/refresh tokens.** Sign-in is
  authentication only, not authorization to call a Google API on the
  user's behalf (no Gmail/Drive/Calendar access is ever requested) -- the
  authorization-code flow's main advantage (obtaining those tokens) buys
  nothing here.
- **Matches this app's own existing shape.** `api/identity_auth.py`'s
  local email/password login already works exactly this way: the frontend
  collects a credential (a password there, an ID token here) and POSTs it
  once; the backend verifies it and issues this app's own session
  (`_issue_tokens`). No second, parallel "redirect flow" architecture is
  introduced.

## CSRF/replay posture, stated explicitly (not silently assumed)

The redirect-based Google flows use a `g_csrf_token` double-submit cookie
specifically because a hidden HTML form POST can be forged cross-site. The
JS-callback flow used here does not have that vector the same way: a
malicious page cannot invoke *this app's own* registered JS callback with
a forged credential, because Google's own client library only ever issues
a credential after verifying the calling page's origin against the
Google Cloud Console-configured "Authorized JavaScript origins" for this
client ID -- an attacker's page, running on a different origin, cannot
obtain a credential scoped to this app's origin at all, let alone deliver
it into this app's page. **What this flow does *not* eliminate on its
own**: a captured, still-valid ID token (e.g. exfiltrated between the
browser and this app's own backend, or from a compromised client) could in
principle be replayed against `POST /auth/google` again within its short
Google-issued lifetime. Two independent mitigations narrow this, stated
honestly rather than claimed as a complete fix: (1) HTTPS-only transport
end to end is a hard requirement of this design, not optional --a Google
ID token's confidentiality in transit is the base assumption every other
control here builds on; (2) the optional, single-use, server-issued
`nonce` (`issue_signin_nonce`/`consume_signin_nonce` below) — when the
frontend requests one and passes it into `google.accounts.id.initialize`,
a captured token cannot be replayed even within its validity window, since
the nonce is deleted from the server the instant it's first consumed.
Google's ID tokens carry no `jti`, so nonce is the only single-use control
available here.

## What this module does NOT do

It does not create/link a local user (that's
`identity/repositories/external_identities.py`, called by `api
/identity_auth.py`'s `POST /auth/google` route with this module's return
value) and it is not a general-purpose OIDC relying party the way
`security/oidc.py` is -- that module validates a bearer token some
*already-deployed third-party* identity provider issued for ongoing API
calls; this module validates a *sign-in-time* Google ID token specifically,
whose job ends the moment this app's own session is issued. The two are
deliberately not merged (see `security/oidc.py`'s own docstring for its
scope).
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.id_token import verify_oauth2_token

from config.settings import Settings
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)

# A real Google ID token is comfortably under 2KB; this is generous
# headroom, not a tight fit -- the point is rejecting an absurdly large
# input (a DoS/parsing-abuse attempt) before it ever reaches a JWT parser,
# per the task's own "bounded length ... reject malformed input safely"
# requirement.
_MAX_CREDENTIAL_LENGTH = 8192

# Reused across calls -- `google.auth.transport.requests.Request` wraps a
# `requests.Session` internally, so this is the same "build the HTTP
# client once, not per call" posture every other outbound client in this
# codebase already follows (e.g. `agent.llm_client._get_ollama_client`).
_google_auth_request = GoogleAuthRequest()


class GoogleTokenValidationError(Exception):
    """Raised for any Google ID token validation failure.

    Always carries a short, non-technical message safe to return directly
    in an HTTP response body -- never the underlying library exception's
    raw text, which can echo back header/claim values from the
    (attacker-controlled) token itself. Mirrors `security.oidc
    .TokenValidationError`'s identical contract.
    """


@dataclass(frozen=True)
class GoogleIdentityClaims:
    """The verified result of a Google ID token -- never constructed except
    by `verify_google_id_token` below, and never holding anything from an
    unverified token.

    Attributes:
        sub: Google's stable, permanent subject identifier for this
            account -- the *only* field ever used as a durable identity
            key (see `identity.models.ExternalIdentity`'s own docstring
            for why email/name/picture are not).
        email: The Google account's current email address. Always present
            when the `email` scope was granted (this app always requests
            it), but see `email_verified` below before treating it as
            confirmed.
        email_verified: Google's own claim that `email` is verified.
            **Not assumed true** -- read directly from the token, default
            `False` if the claim is absent entirely (fail closed, per the
            task's own "do not assume every Google profile has all
            optional claims" requirement).
        name: The account's display name, if Google returned one. Passed
            through `identity.display_name.validate_display_name` by the
            caller, not here -- this dataclass only reports what the token
            said, verified but unvalidated-for-this-app's-own-rules.
        hd: The verified Google Workspace hosted-domain claim, if present.
            `None` for an ordinary consumer Google account.
    """

    sub: str
    email: str
    email_verified: bool
    name: str | None
    hd: str | None


def _log_rejection(reason: str) -> None:
    log_security_event(
        "google_signin_token_rejected",
        "warning",
        "A Google ID token was rejected during sign-in.",
        reason=reason,
    )


def verify_google_id_token(
    credential: str, settings: Settings, *, require_known_nonce: bool = True
) -> GoogleIdentityClaims:
    """Validates `credential` as a Google-issued ID token, returning the
    verified identity claims on success.

    Every check below runs *before* any claim is treated as trusted
    identity information -- this function either returns a fully-verified
    `GoogleIdentityClaims` or raises, there is no partial/best-effort
    result.

    1. **Format/length** -- rejects empty, non-string-shaped, or
       oversized input before it ever reaches a JWT parser.
    2. **Signature, `iss`, `exp`/`iat`** -- delegated entirely to
       `google.oauth2.id_token.verify_oauth2_token`, Google's own
       maintained verifier (deliberately not hand-rolled -- see this
       module's own docstring). It fetches Google's rotating public certs
       over HTTPS from a **fixed, hardcoded Google endpoint**
       (`google.oauth2.id_token._GOOGLE_OAUTH2_CERTS_URL`) -- never a
       token-declared `jku`/`x5u`, and never a token-declared `alg` (the
       algorithm is determined by whichever of Google's own published keys
       matches the token's `kid`, not by the token's own header claim).
       Checks `iss` against Google's own fixed issuer allowlist
       (`accounts.google.com`/`https://accounts.google.com`) internally.
    3. **`aud`** -- checked by the same call, against
       `settings.google_oauth_client_id` (an explicit single-client
       allowlist, never a wildcard).
    4. **`azp`**, if present -- must equal the configured client ID. Some
       Google flows include this claim when the token's issuer and
       audience aren't identical; for this app's single-client-ID setup
       they always should be, so any mismatch is treated as suspicious.
    5. **`sub`** -- required, non-empty.
    6. **`email_verified`** -- read as-is, never assumed true (see
       `GoogleIdentityClaims.email_verified`'s own docstring). Callers
       needing a verified email for a sensitive operation must check this
       field themselves; this function does not reject an unverified
       email on its own, since an unverified-but-authenticated Google
       identity is still a legitimate `sub` to sign in with.
    7. **`hd`**, only if `settings.google_oauth_allowed_hosted_domains` is
       non-empty -- rejects any token whose `hd` claim isn't in that
       allowlist (or is absent). Never inferred from the `email` claim's
       domain suffix.
    8. **`nonce`**, when `require_known_nonce` is True (the default) --
       the token's own `nonce` claim (embedded and signed by Google at
       issuance time, from whatever value the frontend passed into
       `google.accounts.id.initialize({nonce, ...})`) must match a nonce
       this server itself issued and hasn't already consumed
       (`consume_signin_nonce` below, which also deletes it -- single-use).
       No separate "expected nonce" needs to travel with the request
       body; the claim inside the *verified* token is self-authenticating.
       Pass `require_known_nonce=False` only for a caller that
       deliberately doesn't use the nonce endpoint (defense-in-depth is
       optional here, see this module's own docstring on what it does and
       does not protect against).

    Raises:
        GoogleTokenValidationError: on any validation failure, or if
            Google sign-in isn't configured at all (`google_oauth_client_id
            is None`).
    """
    if settings.google_oauth_client_id is None:
        raise GoogleTokenValidationError("Google sign-in is not configured.")

    if not isinstance(credential, str) or not credential:
        _log_rejection("empty_or_malformed_input")
        raise GoogleTokenValidationError("Invalid Google sign-in credential.")
    if len(credential) > _MAX_CREDENTIAL_LENGTH:
        _log_rejection("oversized_input")
        raise GoogleTokenValidationError("Invalid Google sign-in credential.")

    try:
        claims = verify_oauth2_token(
            credential,
            _google_auth_request,
            audience=settings.google_oauth_client_id,
            clock_skew_in_seconds=settings.google_oauth_clock_skew_seconds,
        )
    except GoogleAuthError as exc:
        _log_rejection("issuer_rejected")
        raise GoogleTokenValidationError("Google sign-in verification failed.") from exc
    except ValueError as exc:
        # verify_oauth2_token raises a bare ValueError for essentially
        # every other failure mode (bad signature, expired, wrong
        # audience, malformed JWT structure, JWKS fetch failure) -- the
        # library does not expose distinct exception subtypes for these,
        # so this one generic branch is the correct, complete catch, not
        # a shortcut. The original message is logged nowhere and never
        # reaches the caller -- it can echo token-derived text.
        _log_rejection("token_verification_failed")
        raise GoogleTokenValidationError("Google sign-in verification failed.") from exc

    azp = claims.get("azp")
    if azp is not None and azp != settings.google_oauth_client_id:
        _log_rejection("azp_mismatch")
        raise GoogleTokenValidationError("Google sign-in verification failed.")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        _log_rejection("missing_subject")
        raise GoogleTokenValidationError("Google sign-in verification failed.")

    email = claims.get("email")
    if not isinstance(email, str) or not email:
        _log_rejection("missing_email")
        raise GoogleTokenValidationError("Google sign-in verification failed.")

    email_verified = bool(claims.get("email_verified", False))

    hosted_domain = claims.get("hd")
    if settings.google_oauth_allowed_hosted_domains and (
        not isinstance(hosted_domain, str)
        or hosted_domain not in settings.google_oauth_allowed_hosted_domains
    ):
        _log_rejection("hosted_domain_not_allowed")
        raise GoogleTokenValidationError("Google sign-in verification failed.")

    if require_known_nonce:
        token_nonce = claims.get("nonce")
        if (
            not isinstance(token_nonce, str)
            or not token_nonce
            or not consume_signin_nonce(token_nonce)
        ):
            _log_rejection("nonce_missing_or_unknown")
            raise GoogleTokenValidationError("Google sign-in verification failed.")

    name = claims.get("name")

    return GoogleIdentityClaims(
        sub=subject,
        email=email,
        email_verified=email_verified,
        name=name if isinstance(name, str) and name else None,
        hd=hosted_domain if isinstance(hosted_domain, str) else None,
    )


# --- Sign-in nonce: short-lived, single-use, server-issued -------------
#
# `GET /auth/google/nonce` hands one of these to the frontend before it
# calls `google.accounts.id.initialize({nonce, ...})`, so the resulting ID
# token's own `nonce` claim can be checked against a value this server
# actually generated -- see this module's own docstring for exactly what
# this does and does not protect against. There is no session to tie it to
# yet (pre-authentication) -- a nonce is scoped to one page load, not one
# user, and consumed (deleted) the instant it's checked, successfully or
# not, so it can never be checked twice. Bounded, in-memory,
# process-lifetime only -- the same disclosed "single-process, no
# cross-instance sharing" tradeoff every other bounded cache in this
# codebase already has (`attachments.ai_edit._idempotency_cache`,
# `media_gen.cache.MediaCache`).
_NONCE_MAX_ENTRIES = 1000
_nonce_store: OrderedDict[str, float] = OrderedDict()


def issue_signin_nonce(settings: Settings) -> str:
    """Generates, stores, and returns a new single-use sign-in nonce."""
    nonce = secrets.token_urlsafe(32)
    if len(_nonce_store) >= _NONCE_MAX_ENTRIES:
        _nonce_store.popitem(last=False)
    _nonce_store[nonce] = time.monotonic() + settings.google_oauth_nonce_ttl_seconds
    return nonce


def consume_signin_nonce(nonce: str) -> bool:
    """Returns True and deletes `nonce` if it was issued and not yet
    expired/consumed; False otherwise. Always removes it from the store
    when present, regardless of outcome, so a nonce is never checkable a
    second time."""
    expires_at = _nonce_store.pop(nonce, None)
    if expires_at is None:
        return False
    return time.monotonic() <= expires_at
