"""Direct unit tests for security/tenancy.py. Previously only exercised
indirectly via tests/test_share_policy.py -- this closes that gap (see
01_BASELINE_ARCHITECTURE_AND_REUSE_INVENTORY.md's Prompt 01 finding) and
adds coverage for the new `resolve_tenant_id_for_identity` sibling.
"""

from __future__ import annotations

from security.oidc import AuthIdentity
from security.tenancy import (
    DEFAULT_TENANT_ID,
    resolve_actor_tenant_id,
    resolve_tenant_id_for_identity,
)


class _FakeUser:
    """A minimal stand-in for `identity.models.User` -- `resolve_actor_tenant_id`
    only ever checks `is None`, so no real ORM instance is needed."""


class TestResolveActorTenantId:
    def test_none_user_has_no_tenant(self):
        assert resolve_actor_tenant_id(None) is None

    def test_real_user_gets_the_default_tenant(self):
        assert resolve_actor_tenant_id(_FakeUser()) == DEFAULT_TENANT_ID  # type: ignore[arg-type]


class TestResolveTenantIdForIdentity:
    def test_none_identity_has_no_tenant(self):
        assert resolve_tenant_id_for_identity(None) is None

    def test_real_identity_gets_the_default_tenant(self):
        identity = AuthIdentity(subject="user-123", roles=("user",), mode="oidc")
        assert resolve_tenant_id_for_identity(identity) == DEFAULT_TENANT_ID

    def test_shared_sentinel_identity_still_gets_the_default_tenant_today(self):
        # A "none"/"static_token" auth-mode identity is a shared sentinel,
        # not a genuine per-caller identity -- but this plumbing-only
        # function deliberately doesn't yet distinguish auth_mode (see its
        # own docstring), so it still resolves the same constant.
        identity = AuthIdentity(subject="anonymous", roles=(), mode="none")
        assert resolve_tenant_id_for_identity(identity) == DEFAULT_TENANT_ID
