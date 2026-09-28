"""Unit tests for identity/repositories/shares.py -- against a real
in-memory SQLite database, same convention as
tests/test_identity_repository_history.py. Covers the snapshot boundary,
optimistic concurrency, token lifecycle (entropy/hash-only storage/
expiry/revocation/regeneration), invitation email-binding, and the
projection builder's field/attachment allowlist.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from identity.exceptions import (
    ShareInvitationEmailMismatchError,
    ShareVersionConflictError,
    TokenAlreadyUsedError,
    TokenExpiredError,
)
from identity.models import Base, User
from identity.repositories.history import append_turn, create_conversation
from identity.repositories.shares import (
    accept_invitation,
    build_share_projection,
    count_active_or_pending_members,
    create_share,
    get_member_for_user,
    get_share_by_conversation_for_owner,
    invite_member,
    list_members,
    reactivate_share,
    regenerate_link,
    remove_member,
    resolve_link_by_raw_token,
    revoke_share,
    update_share,
)
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


def _make_user(session: Session, email: str) -> User:
    user = User(email=email, password_hash="x", display_name=email.split("@")[0])
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


class TestCreateShare:
    def test_snapshot_boundary_excludes_messages_sent_after_sharing(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        append_turn(
            db_session, conversation=conversation, user_id=owner.id, question="Q1", answer_text="A1"
        )

        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        assert share.snapshot_message_sequence == 2  # one prompt + one output

        # A message sent *after* sharing must not appear in the projection.
        append_turn(
            db_session, conversation=conversation, user_id=owner.id, question="Q2", answer_text="A2"
        )
        projection = build_share_projection(db_session, share=share)
        contents = [t.content for t in projection.turns]
        assert "Q1" in contents and "A1" in contents
        assert "Q2" not in contents and "A2" not in contents

    def test_refresh_snapshot_picks_up_later_messages(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        append_turn(
            db_session, conversation=conversation, user_id=owner.id, question="Q1", answer_text="A1"
        )
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        append_turn(
            db_session, conversation=conversation, user_id=owner.id, question="Q2", answer_text="A2"
        )

        updated = update_share(
            db_session,
            share=share,
            expected_version=share.version,
            refresh_snapshot=True,
            conversation=conversation,
        )
        projection = build_share_projection(db_session, share=updated)
        contents = [t.content for t in projection.turns]
        assert "Q2" in contents


class TestOptimisticConcurrency:
    def test_stale_version_is_rejected(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        update_share(db_session, share=share, expected_version=1, status="disabled")
        with pytest.raises(ShareVersionConflictError):
            update_share(db_session, share=share, expected_version=1, status="active")

    def test_revoke_with_stale_version_is_rejected(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        with pytest.raises(ShareVersionConflictError):
            revoke_share(db_session, share=share, expected_version=999)


class TestReactivateAfterRevoke:
    def test_revoked_share_can_be_reshared_and_clears_revocation(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        revoke_share(db_session, share=share, expected_version=share.version)
        assert share.revoked_at is not None

        reactivated = reactivate_share(
            db_session,
            share=share,
            conversation=conversation,
            access_mode="invite_only",
            expiry_days=30,
        )
        assert reactivated.revoked_at is None
        assert reactivated.status == "active"
        # Still the same durable row -- never a second ConversationShare for
        # the same conversation (the unique constraint would reject that).
        refetched = get_share_by_conversation_for_owner(
            db_session, conversation_id=conversation.id, owner_user_id=owner.id
        )
        assert refetched is not None
        assert refetched.id == share.id


class TestShareLinkTokenSecurity:
    def test_raw_token_has_high_entropy_and_is_url_safe(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session,
            conversation=conversation,
            owner=owner,
            tenant_id="t1",
            access_mode="anyone_with_link",
            expiry_days=30,
        )
        _, raw_token = regenerate_link(db_session, share=share)
        # 48 raw bytes, base64url-encoded -> at least 64 characters, well
        # over this feature's own 128-bit minimum (48 bytes = 384 bits).
        assert len(raw_token) >= 60
        assert all(c.isalnum() or c in "-_" for c in raw_token)

    def test_only_the_hash_is_stored_never_the_raw_token(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session,
            conversation=conversation,
            owner=owner,
            tenant_id="t1",
            access_mode="anyone_with_link",
            expiry_days=30,
        )
        link, raw_token = regenerate_link(db_session, share=share)
        assert link.token_hash != raw_token
        assert raw_token not in link.token_hash

    def test_token_resolves_back_to_its_share(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session,
            conversation=conversation,
            owner=owner,
            tenant_id="t1",
            access_mode="anyone_with_link",
            expiry_days=30,
        )
        _, raw_token = regenerate_link(db_session, share=share)
        resolved = resolve_link_by_raw_token(db_session, raw_token=raw_token)
        assert resolved is not None
        resolved_share, resolved_link = resolved
        assert resolved_share.id == share.id

    def test_unknown_token_resolves_to_none(self, db_session: Session):
        assert resolve_link_by_raw_token(db_session, raw_token="not-a-real-token") is None

    def test_regenerating_invalidates_the_previous_token(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session,
            conversation=conversation,
            owner=owner,
            tenant_id="t1",
            access_mode="anyone_with_link",
            expiry_days=30,
        )
        _, first_token = regenerate_link(db_session, share=share)
        _, second_token = regenerate_link(db_session, share=share)
        assert first_token != second_token

        resolved_old = resolve_link_by_raw_token(db_session, raw_token=first_token)
        assert resolved_old is not None
        _, old_link = resolved_old
        assert old_link.revoked_at is not None  # old token's row is revoked...

        resolved_new = resolve_link_by_raw_token(db_session, raw_token=second_token)
        assert resolved_new is not None
        _, new_link = resolved_new
        assert new_link.revoked_at is None  # ...while the new one is live

    def test_switching_away_from_public_link_revokes_the_active_link(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session,
            conversation=conversation,
            owner=owner,
            tenant_id="t1",
            access_mode="anyone_with_link",
            expiry_days=30,
        )
        _, raw_token = regenerate_link(db_session, share=share)
        update_share(
            db_session, share=share, expected_version=share.version, access_mode="invite_only"
        )

        resolved = resolve_link_by_raw_token(db_session, raw_token=raw_token)
        assert resolved is not None
        _, stale_link = resolved
        assert stale_link.revoked_at is not None


class TestInvitationsAndAcceptance:
    def test_invite_then_accept_activates_membership(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        invitee = _make_user(db_session, "invitee@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="invitee@example.com", expiry_days=14
        )
        accepted = accept_invitation(db_session, raw_token=raw_token, accepting_user=invitee)
        assert accepted.status == "active"
        assert accepted.user_id == invitee.id

    def test_repeat_invite_to_same_pending_email_is_idempotent_not_duplicated(
        self, db_session: Session
    ):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        invite_member(db_session, share=share, invited_email="invitee@example.com", expiry_days=14)
        invite_member(db_session, share=share, invited_email="invitee@example.com", expiry_days=14)
        assert count_active_or_pending_members(db_session, share=share) == 1

    def test_accept_with_wrong_account_is_rejected(self, db_session: Session):
        """The invitation was matched to a specific existing account at
        invite time -- a *different*, currently-authenticated account
        (even with the raw token in hand, e.g. a forwarded/scraped link)
        must not be able to redeem it."""
        owner = _make_user(db_session, "owner@example.com")
        invitee = _make_user(db_session, "invitee@example.com")
        attacker = _make_user(db_session, "attacker@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="invitee@example.com", expiry_days=14
        )
        with pytest.raises(ShareInvitationEmailMismatchError):
            accept_invitation(db_session, raw_token=raw_token, accepting_user=attacker)
        # Assert the intended invitee can still legitimately accept it.
        accepted = accept_invitation(db_session, raw_token=raw_token, accepting_user=invitee)
        assert accepted.status == "active"

    def test_accept_for_an_email_with_no_existing_account_matches_by_email_on_signup(
        self, db_session: Session
    ):
        """Invited before the invitee had an account -- `user_id` is left
        null at invite time; whichever account whose *email* matches may
        accept once it exists."""
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="future@example.com", expiry_days=14
        )
        future_user = _make_user(db_session, "future@example.com")
        accepted = accept_invitation(db_session, raw_token=raw_token, accepting_user=future_user)
        assert accepted.user_id == future_user.id

    def test_accept_with_mismatched_email_and_no_prior_user_binding_is_rejected(
        self, db_session: Session
    ):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="future@example.com", expiry_days=14
        )
        someone_else = _make_user(db_session, "someone-else@example.com")
        with pytest.raises(ShareInvitationEmailMismatchError):
            accept_invitation(db_session, raw_token=raw_token, accepting_user=someone_else)

    def test_accept_twice_is_rejected_as_already_used(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        invitee = _make_user(db_session, "invitee@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="invitee@example.com", expiry_days=14
        )
        accept_invitation(db_session, raw_token=raw_token, accepting_user=invitee)
        with pytest.raises(TokenAlreadyUsedError):
            accept_invitation(db_session, raw_token=raw_token, accepting_user=invitee)

    def test_expired_invitation_is_rejected(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        invitee = _make_user(db_session, "invitee@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="invitee@example.com", expiry_days=14
        )
        member = get_member_for_user(db_session, share_id=share.id, user_id=invitee.id)
        # invite_member already resolved user_id (the email matched an
        # existing account), but status stays "pending" until accepted --
        # the policy engine denies a pending member's VIEW regardless of
        # this lookup succeeding (see test_share_policy.py).
        assert member is not None
        assert member.status == "pending"
        # Force expiry directly (avoids a flaky real-time-based test).
        from datetime import UTC, datetime, timedelta

        from identity.models import ShareMember

        row = db_session.query(ShareMember).filter_by(share_id=share.id).one()
        row.invitation_expires_at = datetime.now(UTC) - timedelta(days=1)
        db_session.commit()
        with pytest.raises(TokenExpiredError):
            accept_invitation(db_session, raw_token=raw_token, accepting_user=invitee)


class TestRemoveMember:
    def test_removed_member_no_longer_counts_as_active_or_pending(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        invitee = _make_user(db_session, "invitee@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        _, raw_token = invite_member(
            db_session, share=share, invited_email="invitee@example.com", expiry_days=14
        )
        accept_invitation(db_session, raw_token=raw_token, accepting_user=invitee)
        member = get_member_for_user(db_session, share_id=share.id, user_id=invitee.id)
        assert member is not None
        remove_member(db_session, member=member)
        assert count_active_or_pending_members(db_session, share=share) == 0
        assert list_members(db_session, share=share)[0].status == "revoked"


class TestProjectionAllowlist:
    def test_projection_only_exposes_allowlisted_metadata_fields(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        append_turn(
            db_session,
            conversation=conversation,
            user_id=owner.id,
            question="How many orders?",
            answer_text="There were 42 orders.",
            output_metadata={
                "schema_version": 2,
                "sql": "SELECT COUNT(*) FROM orders",
                "sources_used": ["sql"],
                "query_plan": ["step 1: internal planning detail"],
                "schema_tables": [{"table_name": "internal_secret_table"}],
                "retry_count": 3,
                "failure_explanation": "raw internal exception text",
                "attachment_refs": [
                    {
                        "attachment_id": "att_1",
                        "filename": "report.pdf",
                        "media_type": "application/pdf",
                    }
                ],
            },
        )
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        projection = build_share_projection(db_session, share=share)
        assistant_turn = next(t for t in projection.turns if t.role == "assistant")

        assert assistant_turn.extra["sql"] == "SELECT COUNT(*) FROM orders"
        assert assistant_turn.extra["sources_used"] == ["sql"]
        # Never-projected internal fields must be absent entirely, not just
        # empty -- proves the allowlist, not a blocklist, is what's enforced.
        assert "query_plan" not in assistant_turn.extra
        assert "schema_tables" not in assistant_turn.extra
        assert "retry_count" not in assistant_turn.extra
        assert "failure_explanation" not in assistant_turn.extra
        assert "att_1" in projection.approved_attachment_ids

    def test_attachment_not_referenced_by_any_turn_is_not_approved(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        append_turn(
            db_session,
            conversation=conversation,
            user_id=owner.id,
            question="Q",
            answer_text="A",
            output_metadata={
                "attachment_refs": [
                    {"attachment_id": "att_real", "filename": "f", "media_type": "text/plain"}
                ]
            },
        )
        share = create_share(
            db_session, conversation=conversation, owner=owner, tenant_id="t1", expiry_days=30
        )
        projection = build_share_projection(db_session, share=share)
        assert "att_real" in projection.approved_attachment_ids
        assert "att_unrelated_guessed_id" not in projection.approved_attachment_ids
