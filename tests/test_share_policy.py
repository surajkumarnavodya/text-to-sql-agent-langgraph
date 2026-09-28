"""Pure unit tests for `identity.share_policy.authorize_share_action` --
no database, no HTTP, no repository calls. Duck-typed `SimpleNamespace`
fixtures stand in for `ConversationShare`/`ShareMember`/`ShareLink`/`User`
since the policy engine only ever reads a handful of attributes off each
(see that module's own docstring: zero dependency surface, by design).

Every factory below is annotated to return `Any`, not `SimpleNamespace` --
`authorize_share_action`'s own signature types these parameters as the real
ORM models (`TYPE_CHECKING`-only, per that module's own zero-runtime-
dependency design), which a `SimpleNamespace` can never structurally
satisfy for mypy even though it satisfies the *runtime* duck-typing
contract the policy engine actually relies on. `Any` is the correct,
minimal-noise way to say "this test deliberately doesn't construct a real
ORM instance" once, here, rather than a `# type: ignore` at every one of
this file's call sites.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from identity.share_policy import ShareAction, authorize_share_action

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER_ID = uuid.uuid4()
OTHER_USER_ID = uuid.uuid4()
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def user(user_id: uuid.UUID = OTHER_USER_ID) -> Any:
    return SimpleNamespace(id=user_id)


def owner() -> Any:
    return SimpleNamespace(id=OWNER_ID)


def share(**overrides: Any) -> Any:
    defaults = dict(
        owner_user_id=OWNER_ID,
        tenant_id=TENANT_A,
        status="active",
        access_mode="invite_only",
        revoked_at=None,
        expires_at=NOW + timedelta(days=30),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def member(**overrides: Any) -> Any:
    defaults = dict(role="viewer", status="active", revoked_at=None, expires_at=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def link(**overrides: Any) -> Any:
    defaults = dict(revoked_at=None, expires_at=NOW + timedelta(days=30))
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestOwnerOnlyActions:
    @pytest.mark.parametrize(
        "action",
        [
            ShareAction.UPDATE_SETTINGS,
            ShareAction.INVITE_MEMBER,
            ShareAction.REMOVE_MEMBER,
            ShareAction.REGENERATE_LINK,
            ShareAction.REVOKE,
        ],
    )
    def test_owner_can_perform_every_owner_only_action(self, action):
        decision = authorize_share_action(
            actor=owner(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=action,
            conversation_deleted=False,
            now=NOW,
        )
        assert decision.allowed is True
        assert decision.reason == "owner"

    @pytest.mark.parametrize(
        "action",
        [
            ShareAction.UPDATE_SETTINGS,
            ShareAction.INVITE_MEMBER,
            ShareAction.REMOVE_MEMBER,
            ShareAction.REGENERATE_LINK,
            ShareAction.REVOKE,
        ],
    )
    def test_non_owner_viewer_cannot_perform_owner_only_actions(self, action):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=action,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "not_owner"

    def test_anonymous_caller_cannot_perform_owner_only_actions(self):
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(),
            action=ShareAction.REVOKE,
            conversation_deleted=False,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "authentication_required"

    def test_owner_from_the_wrong_tenant_is_denied(self):
        """Defense in depth: even the recorded owner is denied if their
        live-resolved tenant no longer matches the share's own tenant."""
        decision = authorize_share_action(
            actor=owner(),
            actor_tenant_id=TENANT_B,
            share=share(tenant_id=TENANT_A),
            action=ShareAction.REVOKE,
            conversation_deleted=False,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"


class TestViewerActions:
    @pytest.mark.parametrize("action", [ShareAction.VIEW, ShareAction.DOWNLOAD_ATTACHMENT])
    def test_owner_can_preview_their_own_share(self, action):
        decision = authorize_share_action(
            actor=owner(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=action,
            conversation_deleted=False,
            now=NOW,
        )
        assert decision.allowed is True
        assert decision.reason == "owner"

    def test_active_member_can_view(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is True
        assert decision.reason == "member"

    def test_pending_member_cannot_view(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(status="pending"),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "member_not_active"

    def test_revoked_member_cannot_view(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(status="revoked", revoked_at=NOW),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason in ("member_not_active", "member_revoked")

    def test_expired_member_cannot_view(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(expires_at=NOW - timedelta(days=1)),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "member_expired"

    def test_non_member_cannot_view_invite_only_share(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=None,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "not_member"

    def test_anonymous_caller_cannot_view_invite_only_share(self):
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "authentication_required"

    def test_unknown_member_role_is_denied(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(role="editor"),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "unknown_role"

    def test_missing_tenant_is_denied(self):
        """Incomplete policy (no resolvable tenant for the actor) must fail
        closed, not be silently treated as a tenant match."""
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=None,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_deleted_conversation_denies_every_action_including_owner(self):
        decision = authorize_share_action(
            actor=owner(),
            actor_tenant_id=TENANT_A,
            share=share(),
            action=ShareAction.VIEW,
            conversation_deleted=True,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "conversation_deleted"

    def test_missing_share_is_denied(self):
        decision = authorize_share_action(
            actor=owner(),
            actor_tenant_id=TENANT_A,
            share=None,
            action=ShareAction.VIEW,
            conversation_deleted=False,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "share_not_found"

    def test_revoked_share_denies_every_viewer(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(revoked_at=NOW, status="disabled"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "share_revoked"

    def test_disabled_but_not_revoked_share_denies_viewers(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(status="disabled"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "share_disabled"

    def test_expired_share_denies_viewers(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(expires_at=NOW - timedelta(seconds=1)),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "share_expired"


class TestPublicLinkAccess:
    def test_valid_link_grants_anonymous_view(self):
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(access_mode="anyone_with_link"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            link=link(),
            now=NOW,
        )
        assert decision.allowed is True
        assert decision.reason == "public_link"

    def test_invite_only_share_never_accepts_a_link_alone(self):
        """A presented link is irrelevant when the share was never
        configured to trust one -- only real membership works."""
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(access_mode="invite_only"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            link=link(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "authentication_required"

    def test_revoked_link_is_denied_even_if_the_share_itself_is_fine(self):
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(access_mode="anyone_with_link"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            link=link(revoked_at=NOW),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "link_revoked"

    def test_expired_link_is_denied(self):
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(access_mode="anyone_with_link"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            link=link(expires_at=NOW - timedelta(seconds=1)),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "link_expired"

    def test_missing_link_on_a_public_share_is_denied(self):
        decision = authorize_share_action(
            actor=None,
            actor_tenant_id=None,
            share=share(access_mode="anyone_with_link"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            link=None,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "link_required"

    def test_authenticated_owner_using_the_public_link_is_still_recognized_as_owner(self):
        decision = authorize_share_action(
            actor=owner(),
            actor_tenant_id=TENANT_A,
            share=share(access_mode="anyone_with_link"),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            link=link(),
            now=NOW,
        )
        assert decision.allowed is True
        assert decision.reason == "owner"


class TestCrossTenantIsolation:
    def test_cross_tenant_member_is_denied_even_with_a_valid_looking_membership(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_B,
            share=share(tenant_id=TENANT_A),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_matching_tenant_is_allowed(self):
        decision = authorize_share_action(
            actor=user(),
            actor_tenant_id=TENANT_A,
            share=share(tenant_id=TENANT_A),
            action=ShareAction.VIEW,
            conversation_deleted=False,
            member=member(),
            now=NOW,
        )
        assert decision.allowed is True
