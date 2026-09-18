"""Exceptions for the `identity/` package.

Each carries a `safe_message` (mirrors `agent.exceptions.AgentError`'s own
two-message contract) -- `str(exc)` stays the full internal detail (useful
for logs), `.safe_message` is a short, non-technical sentence safe to
return directly in an HTTP response body. This matters especially here:
`InvalidCredentialsError`'s `safe_message` is *deliberately* identical
regardless of whether the email didn't exist, the password was wrong, or
the account is unverified -- a login endpoint that returns a different
message per case is a classic account-enumeration oracle (the same
"generic failure surface" principle `security/oidc.py`'s
`TokenValidationError` already applies to JWT validation).
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base class for every `identity/` exception.

    `message` is optional -- several call sites (`api/identity_auth.py`'s
    login route in particular) construct a subclass with no arguments
    purely to read its class-level `.safe_message` constant (e.g.
    `InvalidCredentialsError().safe_message`), never intending to actually
    raise that instance. When omitted, `message` defaults to whatever
    `safe_message` resolves to, so `str(exc)` is never blank even for that
    usage pattern.
    """

    safe_message: str = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None, *, safe_message: str | None = None) -> None:
        if safe_message is not None:
            self.safe_message = safe_message
        super().__init__(message if message is not None else self.safe_message)


class IdentityNotConfiguredError(IdentityError):
    """`LOCAL_AUTH_ENABLED=true` but `AUTH_DATABASE_URL` isn't set, or the
    reverse -- a configuration gap, not a caller error."""

    safe_message = "Local authentication is not configured."


# Deliberately one shared, generic message for every login failure --
# see this module's own docstring for why (account-enumeration defense).
_GENERIC_LOGIN_FAILURE = "Incorrect email or password."


class DuplicateUserError(IdentityError):
    """`POST /auth/register`/`POST /admin/users` with an email (or
    username) that already exists."""

    safe_message = "An account with that email already exists."


class InvalidCredentialsError(IdentityError):
    """Wrong password, unknown email, or any other "this login attempt
    should fail" case that must not distinguish *why* in its response --
    see this module's own docstring."""

    safe_message = _GENERIC_LOGIN_FAILURE


class AccountLockedError(IdentityError):
    """The account has tripped `Settings.max_login_attempts` and is still
    within its `Settings.login_lockout_minutes` cooldown window.

    Deliberately its own exception type (not folded into
    `InvalidCredentialsError`) even though the generic-message principle
    still applies at the HTTP-response layer -- callers that need to log a
    more specific `signin_events.failure_reason_code` (without ever
    changing what the *client* sees) still need to tell this case apart
    from an ordinary wrong password.
    """

    safe_message = _GENERIC_LOGIN_FAILURE


class AccountNotActiveError(IdentityError):
    """The account exists and the password was correct, but `users.status`
    isn't `active` (e.g. `pending_verification`, `suspended`, `inactive`).

    Also deliberately returns the same generic message as an ordinary
    credential failure -- confirming "this email exists but is suspended"
    to an unauthenticated caller is itself a (smaller) enumeration leak.
    """

    safe_message = _GENERIC_LOGIN_FAILURE


class RefreshTokenInvalidError(IdentityError):
    """The presented refresh token doesn't match any live session -- either
    it never existed, or it has already expired/been explicitly revoked
    (logged out)."""

    safe_message = "Your session has expired. Please sign in again."


class RefreshTokenReusedError(IdentityError):
    """The presented refresh token matches an already-rotated (or already-
    revoked) session row -- a real signal that a stale token is being
    replayed, either because two callers raced a refresh or because the
    token was stolen. `identity.repositories.sessions.rotate_refresh_token`
    responds by revoking the *entire* session family, not just this one
    row, per this feature's own "detect reuse of rotated/revoked refresh
    tokens and revoke the relevant session family" requirement.
    """

    safe_message = "Your session has expired. Please sign in again."


class PasswordTooWeakError(IdentityError):
    """The supplied password fails `Settings.password_min_length` (or a
    future stronger policy) -- carries the *specific* reason in
    `safe_message` since, unlike login, there's no enumeration risk in
    telling an authenticated-enough-to-be-registering caller their own
    chosen password is too short."""


class TokenExpiredError(IdentityError):
    """A password-reset or email-verification token has expired."""

    safe_message = "This link has expired. Please request a new one."


class TokenAlreadyUsedError(IdentityError):
    """A password-reset or email-verification token has already been
    consumed once -- both are strictly single-use."""

    safe_message = "This link has already been used."


class InvalidTokenError(IdentityError):
    """A password-reset or email-verification token doesn't match any
    issued token at all (wrong value, or never existed)."""

    safe_message = "This link is invalid or has expired."


class LocalTokenValidationError(IdentityError):
    """A locally-issued access JWT failed validation (expired, wrong
    signature, wrong issuer/audience, malformed, ...). Mirrors
    `security.oidc.TokenValidationError`'s own "one generic exception type,
    short non-technical message" contract -- `api/auth.py::verify_api_key`
    catches this the same way it already catches that one, and falls
    through to the next configured authentication mechanism rather than
    leaking which specific check failed.
    """

    safe_message = "Invalid or expired token."
