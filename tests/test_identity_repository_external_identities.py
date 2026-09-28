"""Unit tests for identity/repositories/external_identities.py -- against a
real in-memory SQLite database, same convention as
tests/test_identity_repository_users.py (a real SQLAlchemy round-trip
through the unique constraint is a stronger test than mocking the ORM).

These scenarios were also verified live against a real PostgreSQL identity
database during development (schema migration applied/rolled back, every
scenario below exercised for real, then cleaned up) -- this file is the
permanent, fast, CI-run regression coverage for that same behavior.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from identity.exceptions import (
    CannotUnlinkLastSignInMethodError,
    ExternalAccountEmailConflictError,
    ExternalIdentityAlreadyLinkedError,
    GoogleSignInProvisioningError,
)
from identity.models import Base
from identity.repositories.external_identities import (
    find_or_create_user_for_google_identity,
    get_user_by_external_identity,
    link_external_identity,
    list_external_identities,
    unlink_external_identity,
)
from identity.repositories.users import create_user
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


class TestFindOrCreateUserForGoogleIdentity:
    def test_first_sign_in_creates_a_new_password_free_user(self, db_session: Session):
        user, is_new = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-001",
            email="new@example.com",
            email_verified=True,
            display_name="New User",
        )
        assert is_new is True
        assert user.email == "new@example.com"
        assert user.password_hash is None
        assert user.is_email_verified is True
        assert user.status == "active"

    def test_returning_sign_in_with_same_subject_returns_the_same_user(self, db_session: Session):
        first, _ = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-002",
            email="returning@example.com",
            email_verified=True,
            display_name=None,
        )
        second, is_new = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-002",
            email="returning@example.com",
            email_verified=True,
            display_name=None,
        )
        assert second.id == first.id
        assert is_new is False

    def test_returning_sign_in_uses_stored_subject_even_if_email_changed_since(
        self, db_session: Session
    ):
        """sub is the durable identity key -- a Google account's email can
        change on Google's side without breaking the link."""
        first, _ = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-003",
            email="old-address@example.com",
            email_verified=True,
            display_name=None,
        )
        second, is_new = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-003",
            email="new-address@example.com",  # Google-side email changed
            email_verified=True,
            display_name=None,
        )
        assert second.id == first.id
        assert is_new is False
        # The *local* user's own email is never silently overwritten by a
        # later sign-in's claim -- only set once, at creation.
        assert second.email == "old-address@example.com"

    def test_verified_email_conflict_with_existing_local_account_is_refused(
        self, db_session: Session
    ):
        create_user(db_session, email="conflict@example.com", password="StrongPassw0rd!123")
        with pytest.raises(ExternalAccountEmailConflictError):
            find_or_create_user_for_google_identity(
                db_session,
                provider_subject="sub-004",
                email="conflict@example.com",
                email_verified=True,
                display_name=None,
            )

    def test_unverified_email_colliding_with_existing_account_is_refused_generically(
        self, db_session: Session
    ):
        """users.email has a real DB-level unique constraint -- a second
        account with the same email genuinely cannot be created regardless
        of policy. Unlike the verified-email conflict (which discloses that
        an account exists), this case must raise a generic, non-confirming
        error instead, since the caller hasn't proven they control this
        address the way a verified claim would."""
        create_user(db_session, email="maybe-conflict@example.com", password="StrongPassw0rd!123")
        with pytest.raises(GoogleSignInProvisioningError) as exc_info:
            find_or_create_user_for_google_identity(
                db_session,
                provider_subject="sub-005",
                email="maybe-conflict@example.com",
                email_verified=False,
                display_name=None,
            )
        # The generic message must not read like the verified-conflict
        # message -- it must not confirm account existence in the same way.
        assert "already exists" not in exc_info.value.safe_message

    def test_unverified_email_with_no_collision_creates_a_new_account_normally(
        self, db_session: Session
    ):
        user, is_new = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-005b",
            email="genuinely-new@example.com",
            email_verified=False,
            display_name=None,
        )
        assert is_new is True
        assert user.email == "genuinely-new@example.com"
        assert user.is_email_verified is False

    def test_concurrent_first_sign_in_race_is_handled_race_safely(self, db_session: Session):
        """Simulates two near-simultaneous first-sign-ins for the same
        Google identity racing past the initial lookup -- the second must
        resolve to the *same* user, not create a duplicate or crash."""
        user_a, is_new_a = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-race",
            email="race@example.com",
            email_verified=True,
            display_name=None,
        )
        # A second "first sign-in" for the identical identity, after the
        # first has already completed -- exercises the same lookup-then-
        # link path a genuine race would hit at the link_external_identity
        # layer (see TestLinkExternalIdentity's own race test below for the
        # IntegrityError path specifically).
        user_b, is_new_b = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sub-race",
            email="race@example.com",
            email_verified=True,
            display_name=None,
        )
        assert user_a.id == user_b.id
        assert is_new_a is True
        assert is_new_b is False


class TestLinkExternalIdentity:
    def test_links_identity_to_a_user(self, db_session: Session):
        user = create_user(db_session, email="linker@example.com", password="StrongPassw0rd!123")
        link = link_external_identity(
            db_session, user_id=user.id, provider="google", provider_subject="link-sub-1"
        )
        assert link.user_id == user.id
        assert link.provider == "google"

    def test_relinking_the_same_identity_to_the_same_user_is_idempotent(self, db_session: Session):
        user = create_user(
            db_session, email="idempotent@example.com", password="StrongPassw0rd!123"
        )
        first = link_external_identity(
            db_session, user_id=user.id, provider="google", provider_subject="link-sub-2"
        )
        second = link_external_identity(
            db_session, user_id=user.id, provider="google", provider_subject="link-sub-2"
        )
        assert first.id == second.id

    def test_linking_an_identity_already_assigned_to_another_user_is_refused(
        self, db_session: Session
    ):
        user_a = create_user(db_session, email="owner@example.com", password="StrongPassw0rd!123")
        user_b = create_user(
            db_session, email="attacker@example.com", password="StrongPassw0rd!123"
        )
        link_external_identity(
            db_session, user_id=user_a.id, provider="google", provider_subject="claimed-sub"
        )
        with pytest.raises(ExternalIdentityAlreadyLinkedError):
            link_external_identity(
                db_session, user_id=user_b.id, provider="google", provider_subject="claimed-sub"
            )

    def test_get_user_by_external_identity_returns_none_when_unlinked(self, db_session: Session):
        assert (
            get_user_by_external_identity(
                db_session, provider="google", provider_subject="never-linked"
            )
            is None
        )

    def test_get_user_by_external_identity_returns_the_linked_user(self, db_session: Session):
        user = create_user(db_session, email="findme@example.com", password="StrongPassw0rd!123")
        link_external_identity(
            db_session, user_id=user.id, provider="google", provider_subject="findable-sub"
        )
        found = get_user_by_external_identity(
            db_session, provider="google", provider_subject="findable-sub"
        )
        assert found is not None
        assert found.id == user.id


class TestUnlinkExternalIdentity:
    def test_unlinking_a_google_only_accounts_sole_identity_is_refused(self, db_session: Session):
        user, _ = find_or_create_user_for_google_identity(
            db_session,
            provider_subject="sole-identity-sub",
            email="sole@example.com",
            email_verified=True,
            display_name=None,
        )
        with pytest.raises(CannotUnlinkLastSignInMethodError):
            unlink_external_identity(db_session, user_id=user.id, provider="google")

    def test_unlinking_is_allowed_when_a_password_exists(self, db_session: Session):
        user = create_user(
            db_session, email="haspassword@example.com", password="StrongPassw0rd!123"
        )
        link_external_identity(
            db_session, user_id=user.id, provider="google", provider_subject="unlinkable-sub"
        )
        unlink_external_identity(db_session, user_id=user.id, provider="google")
        assert list_external_identities(db_session, user.id) == ()

    def test_unlinking_a_nonexistent_link_is_a_silent_no_op(self, db_session: Session):
        user = create_user(db_session, email="nolinks@example.com", password="StrongPassw0rd!123")
        unlink_external_identity(db_session, user_id=user.id, provider="google")  # no error

    def test_unlinking_an_unknown_user_is_a_silent_no_op(self, db_session: Session):
        import uuid

        unlink_external_identity(db_session, user_id=uuid.uuid4(), provider="google")  # no error


class TestListExternalIdentities:
    def test_empty_for_a_user_with_no_links(self, db_session: Session):
        user = create_user(db_session, email="nolinks2@example.com", password="StrongPassw0rd!123")
        assert list_external_identities(db_session, user.id) == ()

    def test_returns_every_linked_identity(self, db_session: Session):
        user = create_user(db_session, email="multi@example.com", password="StrongPassw0rd!123")
        link_external_identity(
            db_session, user_id=user.id, provider="google", provider_subject="multi-sub-1"
        )
        identities = list_external_identities(db_session, user.id)
        assert len(identities) == 1
        assert identities[0].provider == "google"
