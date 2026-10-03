"""Pure policy tests for `security/navigation.py` -- Prompt 32.

Each persona in the Prompt 32 mapping is asserted against the real role
permission sets (`agent.authz.permissions_for`, `identity.rbac
.permissions_for_roles` over `SEED_ROLES`), not hand-typed lists. A regression
in a role's grants shows up here as a navigation change, which is exactly what
should happen.

The persona mapping under test:

| Persona | Role |
|---|---|
| Platform Super Admin | platform_admin |
| Tenant Admin | admin |
| SME / Semantic Reviewer | analyst |
| Business Analyst | analyst (same grants; the existing model has no finer role) |
| Business User | user |
| Read-only Viewer | auditor (dashboards only, no questions) |
"""

from __future__ import annotations

import pytest
from identity.rbac import SEED_ROLES, permissions_for_roles
from identity.rbac import Permission as IdentityPermission

from agent.authz import permissions_for
from security.navigation import (
    CAPABILITY_RULES,
    NAV_ITEMS,
    nav_item_by_id,
    nav_item_for_path,
    resolve_navigation,
)

_SEED_GRANTS = {name: granted for name, _, _, granted in SEED_ROLES}


def _navigate(roles: tuple[str, ...], *, local: bool = True):
    """Navigation for a caller holding `roles`. `local=False` mirrors an OIDC or
    static-token caller, who has no DB-backed identity permissions."""
    agent = permissions_for(roles)
    identity_perms = (
        frozenset(p.value for p in permissions_for_roles(_SEED_GRANTS, roles))
        if local
        else frozenset()
    )
    items, capabilities = resolve_navigation(
        agent_permissions=agent, identity_permissions=identity_perms
    )
    return {item.id for item in items}, capabilities


def _screens(roles: tuple[str, ...], *, local: bool = True) -> set[str]:
    return _navigate(roles, local=local)[0]


class TestPersonas:
    def test_platform_super_admin_sees_every_screen(self):
        """The operator holds `platform_admin` *and* `admin`. The two are
        needed together: see the next test for why `platform_admin` alone
        is not enough for the AI workspace."""
        assert _screens(("platform_admin", "admin")) == {item.id for item in NAV_ITEMS}

    def test_platform_admin_alone_has_admin_screens_but_no_ai_workspace(self):
        """`platform_admin` is not a base role (`agent.authz.ROLE_PERMISSIONS`
        does not list it), so on its own it grants no question-answering or
        document access. That is the existing, deliberate tradeoff, now made
        visible in navigation rather than as a broken chat link."""
        screens = _screens(("platform_admin",))
        assert "platform_admin" in screens
        assert "chat" not in screens
        assert "knowledge_sources" not in screens

    def test_tenant_admin_sees_every_screen_except_platform_admin(self):
        screens = _screens(("admin",))
        assert screens == {item.id for item in NAV_ITEMS} - {"platform_admin"}

    def test_sme_reviewer_sees_review_work_and_the_ai_workspace(self):
        assert _screens(("analyst",)) == {
            "chat",
            "knowledge_sources",
            "media_search",
            "recommendations",
            "db_onboarding",
            "semantic_review",
        }

    def test_business_user_sees_the_ai_workspace_only(self):
        assert _screens(("user",)) == {"chat", "knowledge_sources", "media_search"}

    def test_read_only_viewer_sees_the_tenant_dashboard_and_no_questions(self):
        """`auditor` holds the dashboard read permission but not `ask`, so it
        sees the tenant dashboard and nothing it could only use to ask
        questions."""
        assert _screens(("auditor",)) == {"tenant_admin"}

    def test_base_viewer_role_sees_the_ai_workspace_without_sql_execution(self):
        screens, capabilities = _navigate(("viewer",))
        assert screens == {"chat", "knowledge_sources", "media_search"}
        assert capabilities["ask"] is True
        assert capabilities["execute_sql"] is False

    @pytest.mark.parametrize("role", ["support", "manager"])
    def test_helpdesk_and_manager_roles_get_only_what_their_grants_allow(self, role):
        screens = _screens((role,))
        # Neither role holds an AI-feature base role, so the AI workspace is
        # hidden; `manager` holds the tenant dashboard read permission.
        assert "chat" not in screens
        assert ("tenant_admin" in screens) == (role == "manager")


class TestDenyByDefault:
    def test_an_unknown_role_sees_nothing(self):
        screens, capabilities = _navigate(("made_up_role",))
        assert screens == set()
        assert not any(capabilities.values())

    def test_no_roles_sees_nothing(self):
        screens, capabilities = _navigate(())
        assert screens == set()
        assert not any(capabilities.values())

    def test_a_non_local_caller_never_gets_identity_screens(self):
        """An OIDC or static-token caller has no DB permissions, so review and
        admin screens (which require a local account on the server) stay hidden
        even when the token carries `admin`."""
        screens = _screens(("admin",), local=False)
        assert screens.isdisjoint(
            {
                "db_onboarding",
                "semantic_review",
                "recommendations",
                "tenant_admin",
                "platform_admin",
            }
        )
        assert "chat" in screens

    def test_nothing_is_visible_with_no_permissions_at_all(self):
        visible, _ = resolve_navigation(
            agent_permissions=frozenset(), identity_permissions=frozenset()
        )
        assert visible == []


class TestNavigationAndCapabilitiesAgree:
    @pytest.mark.parametrize(
        "role",
        ["viewer", "user", "analyst", "admin", "auditor", "manager", "support", "platform_admin"],
    )
    def test_a_screen_is_visible_only_when_some_capability_or_base_grant_backs_it(self, role):
        """A screen's visibility is always backed by the same permission a
        capability or route check uses, so no screen can be visible for a
        reason the rest of the policy does not also grant."""
        screens, capabilities = _navigate((role,))
        if "db_onboarding" in screens:
            assert capabilities["manage_onboarding"] or capabilities["review_onboarding"]
        if "semantic_review" in screens:
            assert capabilities["manage_catalog"] or capabilities["review_catalog"]
        if "recommendations" in screens:
            assert capabilities["review_recommendations"] or capabilities["manage_recommendations"]
        if "platform_admin" in screens:
            assert capabilities["platform_admin"]

    def test_every_capability_name_is_unique(self):
        names = [rule.name for rule in CAPABILITY_RULES]
        assert len(names) == len(set(names))

    def test_every_capability_maps_to_exactly_one_permission(self):
        for rule in CAPABILITY_RULES:
            assert (rule.agent is None) != (rule.identity is None)

    def test_capability_map_is_complete_for_every_caller(self):
        _, capabilities = _navigate(())
        assert set(capabilities) == {rule.name for rule in CAPABILITY_RULES}


class TestPathRegistry:
    def test_the_recommendation_screen_is_not_at_the_api_list_path(self):
        """`/recommendations` is the governance API's list route. The screen must
        live elsewhere, or a page reload would reach JSON."""
        assert nav_item_for_path("/recommendations") is None
        assert nav_item_by_id("recommendations").path == "/recommended-actions"

    def test_known_paths_resolve_and_unknown_paths_do_not(self):
        assert nav_item_for_path("/platform-admin").id == "platform_admin"
        assert nav_item_for_path("/not-a-screen") is None

    def test_every_screen_path_is_unique(self):
        paths = [item.path for item in NAV_ITEMS]
        assert len(paths) == len(set(paths))

    def test_every_identity_permission_referenced_exists(self):
        valid = {p.value for p in IdentityPermission}
        for item in NAV_ITEMS:
            assert item.identity_any <= valid
