"""Unit tests for recommendation/governance_policy.py (Prompt 18,
`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`) -- the recommendation-
governance RBAC+ABAC decision engine. Fully offline, plain values only --
no DB, no HTTP, mirroring `tests/test_semantic_catalog_policy.py`'s own
"pure policy" posture.
"""

from __future__ import annotations

from identity.rbac import Permission
from recommendation.governance_policy import RecommendationAction, authorize_recommendation_action

_REVIEW = frozenset({Permission.RECOMMENDATION_REVIEW})
_MANAGE = frozenset({Permission.RECOMMENDATION_MANAGE})
_BOTH = _REVIEW | _MANAGE
_NONE: frozenset[str] = frozenset()


class TestView:
    def test_review_permission_is_allowed(self):
        decision = authorize_recommendation_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is True
        assert decision.reason == "recommendation_review_or_manage"

    def test_manage_permission_is_also_allowed(self):
        decision = authorize_recommendation_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is True

    def test_no_permissions_is_denied(self):
        decision = authorize_recommendation_action(
            actor_permissions=_NONE,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"


class TestSubmitFeedbackAndMarkResolved:
    def test_review_permission_allows_feedback(self):
        decision = authorize_recommendation_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.SUBMIT_FEEDBACK,
        )
        assert decision.allowed is True

    def test_review_permission_allows_mark_resolved(self):
        decision = authorize_recommendation_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.MARK_RESOLVED,
        )
        assert decision.allowed is True

    def test_manage_alone_also_allows_feedback(self):
        decision = authorize_recommendation_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.SUBMIT_FEEDBACK,
        )
        assert decision.allowed is True

    def test_no_permissions_is_denied(self):
        decision = authorize_recommendation_action(
            actor_permissions=_NONE,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.SUBMIT_FEEDBACK,
        )
        assert decision.allowed is False


class TestExpireIsManageOnly:
    def test_review_permission_alone_is_denied(self):
        decision = authorize_recommendation_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.EXPIRE,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"

    def test_manage_permission_is_allowed(self):
        decision = authorize_recommendation_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.EXPIRE,
        )
        assert decision.allowed is True
        assert decision.reason == "recommendation_manage"

    def test_both_permissions_still_allowed(self):
        decision = authorize_recommendation_action(
            actor_permissions=_BOTH,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.EXPIRE,
        )
        assert decision.allowed is True


class TestTenantIsolation:
    def test_missing_record_tenant_id_is_denied_as_record_not_found(self):
        decision = authorize_recommendation_action(
            actor_permissions=_BOTH,
            actor_tenant_id="tenant-a",
            record_tenant_id=None,
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is False
        assert decision.reason == "record_not_found"

    def test_mismatched_tenant_is_denied_as_cross_tenant(self):
        decision = authorize_recommendation_action(
            actor_permissions=_BOTH,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-b",
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_none_actor_tenant_is_denied_even_with_full_permissions(self):
        """An unresolved actor tenant (e.g. an anonymous/unauthenticated
        caller) must never match any real record_tenant_id, regardless of
        how many permissions are somehow present."""
        decision = authorize_recommendation_action(
            actor_permissions=_BOTH,
            actor_tenant_id=None,
            record_tenant_id="tenant-a",
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_matching_tenant_with_permission_is_allowed(self):
        decision = authorize_recommendation_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=RecommendationAction.VIEW,
        )
        assert decision.allowed is True


class TestUnrecognizedAction:
    def test_denied_with_unrecognized_action_reason(self):
        decision = authorize_recommendation_action(
            actor_permissions=_BOTH,
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action="not_a_real_action",  # type: ignore[arg-type]
        )
        assert decision.allowed is False
        assert decision.reason == "unrecognized_action"
