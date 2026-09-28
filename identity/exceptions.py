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


class ExternalIdentityAlreadyLinkedError(IdentityError):
    """The `(provider, provider_subject)` pair is already linked to a
    *different* local user than the one this call is trying to link it to
    -- e.g. two people racing to be first to sign in with the same Google
    account (vanishingly unlikely, since only the real account owner can
    ever produce a validly-signed token for it), or an authenticated user
    trying to link a Google identity someone else already claimed. Backed
    by a real, database-enforced unique constraint
    (`identity.models.ExternalIdentity`'s own `UniqueConstraint`), not just
    an application-level check a race condition could slip past.
    """

    safe_message = "This Google account is already linked to a different account."


class ExternalAccountEmailConflictError(IdentityError):
    """A Google sign-in attempt's verified email matches an *existing*
    local (password) account that has no Google identity linked yet.

    Deliberately never auto-linked -- see `identity/repositories
    /external_identities.py::find_or_create_user_for_google_identity`'s own
    docstring for why matching email alone is not sufficient proof of
    account ownership. Safe to disclose that *an* account exists here
    (unlike an ordinary login attempt): reaching this path already requires
    a Google-verified, `email_verified=true` token for this exact address,
    which is strictly stronger proof of address ownership than a login
    form's own unauthenticated "email exists" oracle.
    """

    safe_message = (
        "An account with this email already exists. Sign in with your password, "
        "then link Google from your account settings."
    )


class GoogleSignInProvisioningError(IdentityError):
    """A Google sign-in attempt's email matches an existing account, but
    the token's own `email_verified` claim is false -- `users.email` has a
    real, database-level unique constraint, so a second account with the
    same email genuinely cannot be created (this is not a policy choice,
    it's a schema fact), but *unlike* `ExternalAccountEmailConflictError`
    above, this case must not confirm that an account with this email
    exists: the caller has not proven they control this exact address the
    way a Google-verified `email_verified=true` claim would. Deliberately
    the same generic, non-confirming shape as `InvalidCredentialsError`'s
    own account-enumeration defense, applied to this different trigger.
    """

    safe_message = (
        "Google sign-in could not be completed. Please try again, or use a "
        "different sign-in method."
    )


class CannotUnlinkLastSignInMethodError(IdentityError):
    """Unlinking this external identity would leave the account with no way
    to sign in at all -- no password set, and no other linked identity.
    `identity/repositories/external_identities.py::unlink_external_identity`
    checks this before ever deleting the row."""

    safe_message = "Set a password or link another sign-in method before removing this one."


class ShareVersionConflictError(IdentityError):
    """`PATCH .../share` supplied a `version` that no longer matches the
    row's current one -- a concurrent settings change (another browser tab,
    a second owner session) landed first. The optimistic-concurrency
    control this feature's own spec requires; the caller must re-fetch and
    retry, never silently overwrite what the other writer just set."""

    safe_message = "This share was updated elsewhere. Please refresh and try again."


class ShareInvitationEmailMismatchError(IdentityError):
    """The presented invitation token is real, unexpired, and unused, but
    the accepting caller's own account email does not match the address it
    was issued to -- someone other than the intended invitee obtained the
    raw token (a forwarded link, a scraped URL) and tried to redeem it
    under their own account. Deliberately generic and non-confirming, the
    same shape as `InvalidCredentialsError`, so a probing caller cannot use
    the response to learn whether a mismatch or a genuinely invalid token
    was the cause."""

    safe_message = "This invitation is not valid for your account."


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
