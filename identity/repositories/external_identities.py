"""External-provider identity linking (Google sign-in, 2026-09-28).

See `identity/models.py::ExternalIdentity`'s own docstring for the schema
rationale. This module owns every DB-side rule the task's own identity-
linking requirements call for:

- The same `(provider, provider_subject)` always resolves to the same
  local user, across any number of browsers/devices.
- An identity already linked to one user can never be linked to a second
  (a real, database-enforced unique constraint, not just an
  application-level check a race condition could slip past).
- A brand-new Google sign-in never silently merges into an existing
  *local* (password) account just because the email happens to match --
  matching email is not proof of ownership; only a currently-authenticated
  local session choosing to link its own account is (see
  `link_external_identity`'s own docstring for that authenticated-linking
  path, used by `POST /auth/google/link`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from identity.exceptions import (
    CannotUnlinkLastSignInMethodError,
    DuplicateUserError,
    ExternalAccountEmailConflictError,
    ExternalIdentityAlreadyLinkedError,
    GoogleSignInProvisioningError,
)
from identity.models import ExternalIdentity, User
from identity.rbac import DEFAULT_ROLE_NAME
from identity.repositories.users import create_user, get_user_by_email, normalize_email


def get_user_by_external_identity(
    session: Session, *, provider: str, provider_subject: str
) -> User | None:
    identity_row = session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.provider_subject == provider_subject,
        )
    )
    return identity_row.user if identity_row is not None else None


def list_external_identities(session: Session, user_id: uuid.UUID) -> tuple[ExternalIdentity, ...]:
    rows = session.scalars(
        select(ExternalIdentity).where(ExternalIdentity.user_id == user_id)
    ).all()
    return tuple(rows)


def link_external_identity(
    session: Session,
    *,
    user_id: uuid.UUID,
    provider: str,
    provider_subject: str,
    email_at_link: str | None = None,
    email_verified_at_link: bool = False,
) -> ExternalIdentity:
    """Links `(provider, provider_subject)` to `user_id`, race-safe via the
    database's own unique constraint rather than a check-then-insert that a
    concurrent request could slip between.

    Used by two call sites with different trust models, both funneling
    through this one function so the race-safety/conflict logic is never
    duplicated: (1) first-ever sign-in with a not-yet-seen Google identity
    (`find_or_create_user_for_google_identity` below, unauthenticated,
    creates the user in the same transaction), and (2) an already
    logged-in local user explicitly linking their own account from
    settings (`POST /auth/google/link` -- authenticated, `user_id` comes
    from the caller's own verified session, never from the request body).

    Raises:
        ExternalIdentityAlreadyLinkedError: this identity is already linked
            to a *different* user than `user_id`.
    """
    existing = session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.provider_subject == provider_subject,
        )
    )
    if existing is not None:
        if existing.user_id == user_id:
            # Idempotent: signing in again with an identity already linked
            # to this same user is a normal returning-user flow, not an error.
            existing.last_used_at = datetime.now(UTC)
            session.commit()
            return existing
        raise ExternalIdentityAlreadyLinkedError(
            f"External identity {provider}:{provider_subject!r} is already linked "
            f"to a different user."
        )

    link = ExternalIdentity(
        user_id=user_id,
        provider=provider,
        provider_subject=provider_subject,
        email_at_link=email_at_link,
        email_verified_at_link=email_verified_at_link,
    )
    session.add(link)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request won the race between our SELECT above and
        # this INSERT (two near-simultaneous first-sign-ins with the same
        # Google identity, e.g. two browser tabs) -- the unique constraint
        # caught it where our own SELECT couldn't. Roll back and resolve
        # the same way a non-racing caller would have seen it originally.
        session.rollback()
        winner = session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == provider,
                ExternalIdentity.provider_subject == provider_subject,
            )
        )
        if winner is not None and winner.user_id == user_id:
            return winner
        raise ExternalIdentityAlreadyLinkedError(
            f"External identity {provider}:{provider_subject!r} is already linked "
            f"to a different user."
        ) from None
    session.refresh(link)
    return link


def unlink_external_identity(session: Session, *, user_id: uuid.UUID, provider: str) -> None:
    """Removes `user_id`'s link to `provider`, refusing if doing so would
    leave the account with no way to sign in at all (no password set, and
    no other linked identity).

    Raises:
        CannotUnlinkLastSignInMethodError: this is the account's only
            usable sign-in method.
    """
    user = session.get(User, user_id)
    if user is None:
        return
    link = session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user_id, ExternalIdentity.provider == provider
        )
    )
    if link is None:
        return

    other_identity_count = session.scalar(
        select(func.count())
        .select_from(ExternalIdentity)
        .where(ExternalIdentity.user_id == user_id, ExternalIdentity.id != link.id)
    )
    if user.password_hash is None and not other_identity_count:
        raise CannotUnlinkLastSignInMethodError(
            f"Cannot unlink {provider!r}: user {user_id} has no password and no "
            f"other linked identity."
        )

    session.delete(link)
    session.commit()


def find_or_create_user_for_google_identity(
    session: Session,
    *,
    provider_subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None,
    role_name: str = DEFAULT_ROLE_NAME,
) -> tuple[User, bool]:
    """The core "sign in or sign up with Google" decision, run inside one
    transaction. Returns `(user, is_new_user)`.

    Order of checks, deliberately in this sequence:

    1. **Already linked** -- `(provider="google", provider_subject)`
       resolves to an existing user regardless of what email that Google
       account currently reports (a user's Google-side email can change;
       `sub` is the only durable identity, see `ExternalIdentity`'s own
       docstring). This is the ordinary returning-user path and never
       touches the email-conflict check below at all.
    2. **Email conflict, checked before attempting creation only when
       Google says the email is verified** -- if no link exists yet and
       `email_verified` is true, and an existing *local* account already
       has this email, this is refused with an informative message rather
       than silently merged: matching email alone is not proof this caller
       controls that local account (see this module's own module
       docstring). The caller should sign in with their password, then use
       the authenticated `link_external_identity` path from account
       settings. Safe to disclose that an account exists in this case --
       reaching it already required a Google-verified claim for this exact
       address, strictly stronger proof than an ordinary login form's own
       "email exists" oracle.
    3. **Email collision with `email_verified=false`** -- `users.email` has
       a real, database-level unique constraint, so a second account with
       the same email genuinely cannot be created regardless of policy;
       this is a schema fact, not a choice. Unlike check 2, this case must
       NOT confirm an account with this email exists (the caller hasn't
       proven they control this address) -- `create_user`'s own
       `DuplicateUserError` is caught and re-raised as the generic,
       non-confirming `GoogleSignInProvisioningError` instead, the same
       account-enumeration defense `InvalidCredentialsError` already
       applies to password login.
    4. **First sign-in with this Google identity, no conflicting local
       account** -- creates a new user with `password_hash=None` (a
       Google-only account) and links the identity, both in the same
       transaction (so a failure partway through never leaves an
       unlinked, orphaned user or a link with no user).

    `display_name` is used as-is if provided (the caller -- `security
    .google_oidc` -- is responsible for having already run it through
    `identity.display_name.validate_display_name` and passing `None` if it
    doesn't validate, so a new Google user simply lands in this app's
    existing "needs profile completion" state rather than this function
    needing its own validation-failure handling).

    Raises:
        ExternalAccountEmailConflictError: see check 2 above.
        GoogleSignInProvisioningError: see check 3 above.
    """
    linked_user = get_user_by_external_identity(
        session, provider="google", provider_subject=provider_subject
    )
    if linked_user is not None:
        link = session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == "google",
                ExternalIdentity.provider_subject == provider_subject,
            )
        )
        if link is not None:
            link.last_used_at = datetime.now(UTC)
            session.commit()
        return linked_user, False

    if email_verified:
        existing_by_email = get_user_by_email(session, email)
        if existing_by_email is not None:
            raise ExternalAccountEmailConflictError(
                f"Email {normalize_email(email)!r} already belongs to an existing "
                f"account not yet linked to this Google identity."
            )

    try:
        new_user = create_user(
            session,
            email=email,
            password=None,
            display_name=display_name,
            status="active",
            role_name=role_name,
        )
    except DuplicateUserError as exc:
        # Only reachable when email_verified is False (the branch above
        # already handled the verified case) -- `users.email` has a real,
        # database-level unique constraint, so a second account with this
        # email genuinely cannot be created regardless of policy. Unlike
        # the verified-email conflict above, this must NOT confirm an
        # account with this email exists (the caller hasn't proven they
        # control this address the way a verified claim would) -- a
        # generic, non-confirming rejection instead, matching
        # InvalidCredentialsError's own account-enumeration defense.
        raise GoogleSignInProvisioningError(
            f"Email {normalize_email(email)!r} collides with an existing account, "
            f"but the Google token's email_verified claim was false."
        ) from exc

    new_user.is_email_verified = email_verified
    session.commit()

    link_external_identity(
        session,
        user_id=new_user.id,
        provider="google",
        provider_subject=provider_subject,
        email_at_link=normalize_email(email),
        email_verified_at_link=email_verified,
    )
    session.refresh(new_user)
    return new_user, True
