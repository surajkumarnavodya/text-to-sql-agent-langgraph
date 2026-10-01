"""Unit tests for onboarding/policy.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- the onboarding RBAC+ABAC decision
engine. Fully offline, plain values only -- no DB, no HTTP, mirroring
`identity/share_policy.py`'s own "pure policy" test posture.
"""

from __future__ import annotations

from identity.rbac import Permission

from onboarding.policy import OnboardingAction, authorize_onboarding_action

_MANAGE = frozenset({Permission.ONBOARDING_MANAGE})
_REVIEW = frozenset({Permission.ONBOARDING_REVIEW})
_NONE: frozenset[str] = frozenset()


class TestCreateJob:
    def test_manage_permission_is_allowed(self):
        decision = authorize_onboarding_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            job_tenant_id=None,
            action=OnboardingAction.CREATE_JOB,
        )
        assert decision.allowed is True

    def test_review_permission_alone_is_denied(self):
        decision = authorize_onboarding_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            job_tenant_id=None,
            action=OnboardingAction.CREATE_JOB,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"

    def test_no_permissions_is_denied(self):
        decision = authorize_onboarding_action(
            actor_permissions=_NONE,
            actor_tenant_id="tenant-a",
            job_tenant_id=None,
            action=OnboardingAction.CREATE_JOB,
        )
        assert decision.allowed is False


class TestTenantIsolation:
    def test_missing_job_tenant_id_is_denied_as_job_not_found(self):
        decision = authorize_onboarding_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            job_tenant_id=None,
            action=OnboardingAction.VIEW_JOB,
        )
        assert decision.allowed is False
        assert decision.reason == "job_not_found"

    def test_mismatched_tenant_is_denied_as_cross_tenant(self):
        decision = authorize_onboarding_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-b",
            action=OnboardingAction.VIEW_JOB,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_missing_actor_tenant_id_is_denied_as_cross_tenant(self):
        decision = authorize_onboarding_action(
            actor_permissions=_MANAGE,
            actor_tenant_id=None,
            job_tenant_id="tenant-b",
            action=OnboardingAction.VIEW_JOB,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_cross_tenant_denial_takes_priority_over_a_permission_check(self):
        """Even a fully-permissioned admin is denied on tenant mismatch --
        tenant isolation is checked before any RBAC branch."""
        decision = authorize_onboarding_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-b",
            action=OnboardingAction.PUBLISH,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"


class TestManageActions:
    def test_manage_permission_allows_every_manage_action(self):
        for action in (
            OnboardingAction.RUN_DISCOVERY,
            OnboardingAction.PUBLISH,
            OnboardingAction.CANCEL,
            OnboardingAction.RETRY,
        ):
            decision = authorize_onboarding_action(
                actor_permissions=_MANAGE,
                actor_tenant_id="tenant-a",
                job_tenant_id="tenant-a",
                action=action,
            )
            assert decision.allowed is True, f"{action} should be allowed for ONBOARDING_MANAGE"

    def test_review_permission_alone_cannot_publish(self):
        decision = authorize_onboarding_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-a",
            action=OnboardingAction.PUBLISH,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"


class TestViewOrReviewActions:
    def test_view_job_allowed_with_manage_permission(self):
        decision = authorize_onboarding_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-a",
            action=OnboardingAction.VIEW_JOB,
        )
        assert decision.allowed is True

    def test_view_job_allowed_with_review_permission(self):
        decision = authorize_onboarding_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-a",
            action=OnboardingAction.VIEW_JOB,
        )
        assert decision.allowed is True

    def test_decide_review_item_allowed_with_review_permission(self):
        decision = authorize_onboarding_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-a",
            action=OnboardingAction.DECIDE_REVIEW_ITEM,
        )
        assert decision.allowed is True

    def test_decide_review_item_denied_with_no_permission(self):
        decision = authorize_onboarding_action(
            actor_permissions=_NONE,
            actor_tenant_id="tenant-a",
            job_tenant_id="tenant-a",
            action=OnboardingAction.DECIDE_REVIEW_ITEM,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"
