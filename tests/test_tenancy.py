"""Direct unit tests for security/tenancy.py.

Originally closed a coverage gap (`01_BASELINE_ARCHITECTURE_AND_REUSE
_INVENTORY.md`'s Prompt 01 finding) when both resolvers still returned one
hardcoded constant. Rewritten for Prompt 20, where a tenant is a real,
persisted, server-resolved fact: the cases below now assert that a tenant
is read from the right *source* and that an unresolvable one fails closed,
rather than that a constant is returned.

The cross-module negative tests ("tenant A cannot reach tenant B's X") live
in `tests/security/test_cross_tenant_isolation.py`; this file stays focused
on `security/tenancy.py`'s own four functions.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from security.oidc import AuthIdentity, extract_tenant_id
from security.tenancy import (
    CLIENT_TENANT_HEADERS,
    DEFAULT_TENANT_ID,
    TenantContext,
    TenantResolutionError,
    reject_client_tenant_override,
    resolve_actor_tenant_id,
    resolve_tenant_id_for_identity,
)


class _FakeUser:
    """A minimal stand-in for `identity.models.User`. `tenant_id` is the only
    attribute `resolve_actor_tenant_id` reads, so no real ORM instance (and
    no database) is needed here -- the ORM-backed cases are in
    `tests/security/test_cross_tenant_isolation.py`."""

    def __init__(self, tenant_id: str | None = None) -> None:
        self.tenant_id = tenant_id


class TestResolveActorTenantId:
    def test_none_user_has_no_tenant(self):
        assert resolve_actor_tenant_id(None) is None

    def test_reads_the_users_own_tenant_column(self):
        assert resolve_actor_tenant_id(_FakeUser("tenant_a")) == "tenant_a"  # type: ignore[arg-type]

    def test_a_user_row_without_a_tenant_falls_back_to_the_default_tenant(self):
        """A pre-Prompt-20 row (or a lightweight test stand-in) resolves to
        the default tenant -- the most restrictive answer available, never a
        widening, since that is simply where every existing row lives."""
        assert resolve_actor_tenant_id(_FakeUser(None)) == DEFAULT_TENANT_ID  # type: ignore[arg-type]

    def test_an_object_with_no_tenant_attribute_at_all_still_resolves(self):
        class _Bare:
            pass

        assert resolve_actor_tenant_id(_Bare()) == DEFAULT_TENANT_ID  # type: ignore[arg-type]


class TestResolveTenantIdForIdentity:
    def test_none_identity_has_no_tenant(self):
        assert resolve_tenant_id_for_identity(None) is None

    def test_reads_the_identitys_verified_tenant(self):
        identity = AuthIdentity(
            subject="user-123", roles=("user",), mode="local", tenant_id="tenant_a"
        )
        assert resolve_tenant_id_for_identity(identity) == "tenant_a"

    def test_an_identity_constructed_without_a_tenant_defaults(self):
        """`AuthIdentity.tenant_id` has a default precisely so every
        pre-Prompt-20 construction site keeps producing the identity it did
        before."""
        identity = AuthIdentity(subject="user-123", roles=("user",), mode="oidc")
        assert resolve_tenant_id_for_identity(identity) == DEFAULT_TENANT_ID

    def test_shared_sentinel_identity_gets_the_default_tenant(self):
        """`none`/`static_token` callers are indistinguishable by design, so
        there is no per-caller tenant to resolve -- documented in
        `docs/MULTI_TENANCY.md` as a deployment shape where tenant isolation
        is not a meaningful boundary, rather than papered over."""
        identity = AuthIdentity(subject="anonymous", roles=(), mode="none")
        assert resolve_tenant_id_for_identity(identity) == DEFAULT_TENANT_ID


class TestExtractTenantIdFromOidcClaims:
    def test_unconfigured_claim_resolves_to_the_default_tenant(self):
        assert extract_tenant_id({"tenant": "acme"}, None) == DEFAULT_TENANT_ID

    def test_reads_the_configured_claim(self):
        assert extract_tenant_id({"tenant": "acme"}, "tenant") == "acme"

    @pytest.mark.parametrize("value", [None, "", 42, [], {}])
    def test_a_missing_or_non_string_claim_falls_back_rather_than_failing(self, value):
        """Failing the request outright would lock out every caller the moment
        one IdP stopped emitting the claim; the default tenant cannot widen
        access, so falling back is the safer failure mode."""
        assert extract_tenant_id({"tenant": value}, "tenant") == DEFAULT_TENANT_ID


class TestRejectClientTenantOverride:
    @pytest.mark.parametrize("header", sorted(CLIENT_TENANT_HEADERS))
    def test_refuses_every_tenant_shaped_header(self, header):
        with pytest.raises(TenantResolutionError):
            reject_client_tenant_override({header: "tenant_b"})

    def test_allows_a_request_with_no_tenant_header(self):
        reject_client_tenant_override({"authorization": "Bearer x"})

    def test_a_non_container_is_tolerated_rather_than_raising(self):
        """Defensive: this guard must never be the reason a request fails for
        an unrelated reason."""
        reject_client_tenant_override(object())


class TestTenantContext:
    def test_is_frozen(self):
        """A resolved context is proof-of-work: nothing downstream may mutate
        it into a different tenant after the status check has passed."""
        context = TenantContext(tenant_id="tenant_a", name="Tenant A")
        with pytest.raises(FrozenInstanceError):
            context.tenant_id = "tenant_b"  # type: ignore[misc]
