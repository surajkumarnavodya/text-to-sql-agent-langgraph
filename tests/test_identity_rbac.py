"""Unit tests for identity/rbac.py -- the granular permission seed data and
role-bridging contract with agent.authz."""

from __future__ import annotations

from identity.rbac import (
    BASE_ROLE_NAMES,
    DEFAULT_ROLE_NAME,
    SEED_PERMISSIONS,
    SEED_ROLES,
    Permission,
    permissions_for_roles,
)

from agent.authz import ROLE_PERMISSIONS as AGENT_AUTHZ_ROLE_PERMISSIONS


class TestSeedDataConsistency:
    def test_every_role_only_grants_seeded_permissions(self):
        seeded_codes = {code for code, _ in SEED_PERMISSIONS}
        for _name, _description, _is_system, granted in SEED_ROLES:
            assert granted <= seeded_codes

    def test_role_names_are_unique(self):
        names = [name for name, *_ in SEED_ROLES]
        assert len(names) == len(set(names))

    def test_permission_codes_are_unique(self):
        codes = [code for code, _ in SEED_PERMISSIONS]
        assert len(codes) == len(set(codes))

    def test_every_seed_role_is_marked_system_role(self):
        assert all(is_system for _name, _description, is_system, _granted in SEED_ROLES)


class TestBaseRoleBridge:
    def test_base_role_names_match_agent_authz_exactly(self):
        """The whole point of this bridge: a locally-authenticated user's
        role names must be recognized by agent.authz.ROLE_PERMISSIONS
        unchanged, or every existing AI/RAG/SQL route would silently grant
        zero permissions to a local user with one of these roles."""
        assert set(AGENT_AUTHZ_ROLE_PERMISSIONS.keys()) == BASE_ROLE_NAMES

    def test_default_role_is_a_base_role(self):
        assert DEFAULT_ROLE_NAME in BASE_ROLE_NAMES

    def test_seed_roles_include_every_base_role_name(self):
        seeded_names = {name for name, *_ in SEED_ROLES}
        assert seeded_names >= BASE_ROLE_NAMES


class TestPermissionProgression:
    def _granted(self, role_name: str) -> frozenset[Permission]:
        return next(granted for name, _d, _s, granted in SEED_ROLES if name == role_name)

    def test_user_is_a_superset_of_viewer(self):
        assert self._granted("viewer") <= self._granted("user")

    def test_analyst_is_a_superset_of_user(self):
        assert self._granted("user") <= self._granted("analyst")

    def test_admin_is_a_superset_of_analyst(self):
        assert self._granted("analyst") <= self._granted("admin")

    def test_only_admin_and_platform_admin_get_user_management_permissions(self):
        """`platform_admin` (Prompt 28) is a deliberate superset of `admin`
        (see `identity.rbac._PLATFORM_ADMIN`'s own docstring) -- the only
        other role allowed to carry these permissions, since every other
        non-admin role granting them would be a real privilege-escalation
        regression."""
        management_perms = {
            Permission.USERS_CREATE,
            Permission.USERS_UPDATE,
            Permission.USERS_DEACTIVATE,
            Permission.USERS_ASSIGN_ROLES,
        }
        for name, _d, _s, granted in SEED_ROLES:
            if name in ("admin", "platform_admin"):
                assert management_perms <= granted
            else:
                assert not (
                    management_perms & granted
                ), f"{name} should not have {management_perms}"

    def test_platform_admin_is_a_superset_of_admin(self):
        assert self._granted("admin") <= self._granted("platform_admin")

    def test_platform_admin_permission_is_granted_only_to_platform_admin_role(self):
        """`PLATFORM_ADMIN` is the one permission deliberately exempt from
        the ordinary tenant-scoped admin set -- a tenant's own `admin`
        account must never hold it without a separate, explicit role
        grant (see `identity.rbac.Permission.PLATFORM_ADMIN`'s own
        docstring for the full "platform-admin versus tenant-admin"
        rationale)."""
        for name, _d, _s, granted in SEED_ROLES:
            if name == "platform_admin":
                assert Permission.PLATFORM_ADMIN in granted
            else:
                assert Permission.PLATFORM_ADMIN not in granted, f"{name} should not have it"


class TestPermissionsForRoles:
    def test_unions_across_roles(self):
        role_map = {
            "a": frozenset({Permission.AI_USE}),
            "b": frozenset({Permission.RAG_QUERY}),
        }
        result = permissions_for_roles(role_map, ("a", "b"))
        assert result == {Permission.AI_USE, Permission.RAG_QUERY}

    def test_unknown_role_contributes_nothing(self):
        role_map = {"a": frozenset({Permission.AI_USE})}
        assert permissions_for_roles(role_map, ("not-a-real-role",)) == frozenset()

    def test_empty_roles_grants_nothing(self):
        role_map = {"a": frozenset({Permission.AI_USE})}
        assert permissions_for_roles(role_map, ()) == frozenset()
