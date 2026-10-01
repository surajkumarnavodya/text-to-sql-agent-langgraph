"""Unit tests for semantic/catalog_policy.py (Prompt 09,
`09_SEMANTIC_CATALOG_CONTRACT.md`) -- the semantic-catalog RBAC+ABAC
decision engine. Fully offline, plain values only -- no DB, no HTTP,
mirroring `tests/test_onboarding_policy.py`'s own "pure policy" posture.
"""

from __future__ import annotations

from identity.rbac import Permission
from semantic.catalog_policy import CatalogAction, authorize_catalog_action

_MANAGE = frozenset({Permission.CATALOG_MANAGE})
_REVIEW = frozenset({Permission.CATALOG_REVIEW})
_NONE: frozenset[str] = frozenset()


class TestCreateEntry:
    def test_manage_permission_is_allowed(self):
        decision = authorize_catalog_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            entry_tenant_id=None,
            action=CatalogAction.CREATE_ENTRY,
        )
        assert decision.allowed is True

    def test_review_permission_alone_is_denied(self):
        decision = authorize_catalog_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            entry_tenant_id=None,
            action=CatalogAction.CREATE_ENTRY,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"

    def test_no_permissions_is_denied(self):
        decision = authorize_catalog_action(
            actor_permissions=_NONE,
            actor_tenant_id="tenant-a",
            entry_tenant_id=None,
            action=CatalogAction.CREATE_ENTRY,
        )
        assert decision.allowed is False


class TestTenantIsolation:
    def test_missing_entry_tenant_id_is_denied_as_entry_not_found(self):
        decision = authorize_catalog_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            entry_tenant_id=None,
            action=CatalogAction.VIEW_ENTRY,
        )
        assert decision.allowed is False
        assert decision.reason == "entry_not_found"

    def test_mismatched_tenant_is_denied_as_cross_tenant(self):
        decision = authorize_catalog_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-b",
            action=CatalogAction.VIEW_ENTRY,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_missing_actor_tenant_id_is_denied_as_cross_tenant(self):
        decision = authorize_catalog_action(
            actor_permissions=_MANAGE,
            actor_tenant_id=None,
            entry_tenant_id="tenant-b",
            action=CatalogAction.VIEW_ENTRY,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"

    def test_cross_tenant_denial_takes_priority_over_a_permission_check(self):
        """Even a fully-permissioned admin is denied on tenant mismatch --
        tenant isolation is checked before any RBAC branch."""
        decision = authorize_catalog_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-b",
            action=CatalogAction.PUBLISH,
        )
        assert decision.allowed is False
        assert decision.reason == "cross_tenant"


class TestManageActions:
    def test_manage_permission_allows_every_manage_action(self):
        for action in (CatalogAction.EDIT_DRAFT, CatalogAction.PUBLISH):
            decision = authorize_catalog_action(
                actor_permissions=_MANAGE,
                actor_tenant_id="tenant-a",
                entry_tenant_id="tenant-a",
                action=action,
            )
            assert decision.allowed is True, f"{action} should be allowed for CATALOG_MANAGE"

    def test_review_permission_alone_cannot_publish(self):
        decision = authorize_catalog_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.PUBLISH,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"

    def test_review_permission_alone_cannot_edit_a_draft(self):
        decision = authorize_catalog_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.EDIT_DRAFT,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"


class TestViewOrReviewActions:
    def test_view_entry_allowed_with_manage_permission(self):
        decision = authorize_catalog_action(
            actor_permissions=_MANAGE,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.VIEW_ENTRY,
        )
        assert decision.allowed is True

    def test_view_entry_allowed_with_review_permission(self):
        decision = authorize_catalog_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.VIEW_ENTRY,
        )
        assert decision.allowed is True

    def test_review_action_allowed_with_review_permission(self):
        decision = authorize_catalog_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.REVIEW,
        )
        assert decision.allowed is True

    def test_request_changes_allowed_with_review_permission(self):
        decision = authorize_catalog_action(
            actor_permissions=_REVIEW,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.REQUEST_CHANGES,
        )
        assert decision.allowed is True

    def test_review_action_denied_with_no_permission(self):
        decision = authorize_catalog_action(
            actor_permissions=_NONE,
            actor_tenant_id="tenant-a",
            entry_tenant_id="tenant-a",
            action=CatalogAction.REVIEW,
        )
        assert decision.allowed is False
        assert decision.reason == "missing_permission"
