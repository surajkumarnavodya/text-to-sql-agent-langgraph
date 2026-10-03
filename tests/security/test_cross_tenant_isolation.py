"""Cross-tenant negative tests -- Prompt 20
(`20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`).

Every test here asserts something a tenant must **not** be able to do. They
live in `tests/security/` (see that directory's own `README.md`) because
they are adversarial-scenario regression tests spanning several modules at
once, not coverage for any single module's own behavior -- the per-module
positive cases live in `tests/test_tenancy.py`,
`tests/test_result_cache.py`, `tests/test_golden_examples.py`,
`tests/test_retrieval.py` and `tests/test_observability_metrics.py`
respectively.

Unlike the pre-Prompt-20 tenant tests in `tests/test_api_shares.py`, these
do **not** monkeypatch `resolve_actor_tenant_id` -- `users.tenant_id` is a
real column now, so the real resolver is exercised against real rows. That
is precisely the gap Prompt 20 closed: before it, every tenant comparison
in this codebase compared one hardcoded constant against itself.
"""

from __future__ import annotations

import uuid

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, Conversation, User
from identity.repositories.history import (
    create_conversation,
    get_conversation,
    list_conversations,
    search_history,
)
from identity.repositories.tenants import create_tenant, set_tenant_status
from identity.repositories.users import create_user
from identity.security import create_access_token, validate_local_token
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
from agent.exceptions import SchemaRetrievalError
from config.settings import DatabaseConnectionConfig, Settings
from embeddings.retriever import select_database
from observability.metrics import PerformanceMetrics
from security.tenancy import (
    DEFAULT_TENANT_ID,
    TenantResolutionError,
    reject_client_tenant_override,
    resolve_actor_tenant_id,
    resolve_tenant_context,
)

_SETTINGS = Settings(
    local_auth_enabled=True,
    auth_database_url="postgresql://placeholder/unused",
    jwt_secret_key="s" * 40,
    jwt_issuer="text-to-sql-agent",
    jwt_audience="text-to-sql-web",
    allow_public_registration=True,
    cookie_secure=False,
    password_min_length=8,
    login_rate_limit_per_minute=1000,
    register_rate_limit_per_hour=1000,
)

_PASSWORD = "correcthorse battery staple 1"


@pytest.fixture
def engine(monkeypatch):
    """A fresh in-memory identity database with the default tenant and RBAC
    seeded, plus two extra tenants to test against."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)  # also calls ensure_default_tenant
    create_tenant(session, tenant_id="tenant_a", name="Tenant A")
    create_tenant(session, tenant_id="tenant_b", name="Tenant B")
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()

    yield engine


@pytest.fixture
def session(engine):
    session = identity_db_mod.get_identity_session(_SETTINGS)
    yield session
    session.close()


@pytest.fixture
def client(engine) -> TestClient:
    return TestClient(api_main.app)


def _user_in(session, email: str, tenant_id: str) -> User:
    return create_user(session, email=email, password=_PASSWORD, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# 1. Tenant resolution itself
# ---------------------------------------------------------------------------


class TestTenantResolutionIsServerSide:
    def test_a_users_tenant_is_read_from_their_own_row(self, session):
        user = _user_in(session, "a@example.com", "tenant_a")
        assert resolve_actor_tenant_id(user) == "tenant_a"

    def test_two_users_in_different_tenants_resolve_differently(self, session):
        """The literal thing that was impossible before Prompt 20: two real
        callers in two different tenants."""
        a = _user_in(session, "a@example.com", "tenant_a")
        b = _user_in(session, "b@example.com", "tenant_b")
        assert resolve_actor_tenant_id(a) != resolve_actor_tenant_id(b)

    def test_an_unknown_tenant_is_refused_not_defaulted(self, session):
        with pytest.raises(TenantResolutionError):
            resolve_tenant_context(session, "no_such_tenant")

    def test_a_suspended_tenant_is_refused(self, session):
        set_tenant_status(session, tenant_id="tenant_a", status="suspended")
        with pytest.raises(TenantResolutionError):
            resolve_tenant_context(session, "tenant_a")

    def test_no_tenant_at_all_is_refused(self, session):
        with pytest.raises(TenantResolutionError):
            resolve_tenant_context(session, None)

    def test_every_refusal_message_is_generic(self, session):
        """A caller must not be able to tell "no such tenant" from
        "suspended" from "not yours" -- anti-enumeration."""
        set_tenant_status(session, tenant_id="tenant_a", status="suspended")

        with pytest.raises(TenantResolutionError) as suspended:
            resolve_tenant_context(session, "tenant_a")
        with pytest.raises(TenantResolutionError) as missing:
            resolve_tenant_context(session, "no_such_tenant")

        assert suspended.value.safe_message == missing.value.safe_message


class TestClientSuppliedTenantIsNeverTrusted:
    @pytest.mark.parametrize(
        "header", ["x-tenant-id", "x-tenant", "tenant-id", "x-tenant-override"]
    )
    def test_the_resolver_guard_refuses_each_tenant_shaped_header(self, header):
        with pytest.raises(TenantResolutionError):
            reject_client_tenant_override({header: "tenant_b"})

    def test_an_ordinary_request_with_no_tenant_header_passes(self):
        reject_client_tenant_override({"authorization": "Bearer x", "accept": "*/*"})

    def test_the_api_refuses_a_request_carrying_a_tenant_header(self, client: TestClient):
        """End to end: the single auth chokepoint rejects it with a 400
        before authenticating anything, so no individual route has to
        remember the check."""
        response = client.get("/schema/tables", headers={"X-Tenant-Id": "tenant_b"})
        assert response.status_code == 400
        assert "client" in response.json()["detail"].lower()


class TestTenantTravelsOnlyInAVerifiedToken:
    def test_the_access_token_carries_the_users_own_tenant(self):
        token = create_access_token("user-1", ("user",), _SETTINGS, tenant_id="tenant_a")
        assert validate_local_token(token, _SETTINGS).tenant_id == "tenant_a"

    def test_a_token_minted_without_a_tenant_resolves_to_the_default_tenant(self):
        """Backward compatibility: a token already in a browser when this
        feature shipped keeps working, with the tenant it implicitly had."""
        token = create_access_token("user-1", ("user",), _SETTINGS)
        assert validate_local_token(token, _SETTINGS).tenant_id == DEFAULT_TENANT_ID

    def test_login_is_refused_while_the_tenant_is_suspended(self, client: TestClient, session):
        """No token may be minted for a suspended tenant -- otherwise a
        suspension could be ridden out for a full token lifetime."""
        _user_in(session, "a@example.com", "tenant_a")
        set_tenant_status(session, tenant_id="tenant_a", status="suspended")

        response = client.post(
            "/auth/login", json={"email": "a@example.com", "password": _PASSWORD}
        )
        assert response.status_code == 401

    def test_a_suspension_denial_is_indistinguishable_from_a_wrong_password(
        self, client: TestClient, session
    ):
        """Otherwise the login endpoint becomes an oracle for which accounts
        exist in which tenant."""
        _user_in(session, "a@example.com", "tenant_a")
        set_tenant_status(session, tenant_id="tenant_a", status="suspended")

        suspended = client.post(
            "/auth/login", json={"email": "a@example.com", "password": _PASSWORD}
        )
        wrong_password = client.post(
            "/auth/login", json={"email": "a@example.com", "password": "wrong password 123"}
        )
        assert suspended.status_code == wrong_password.status_code == 401


# ---------------------------------------------------------------------------
# 2. Persistence: chat history
# ---------------------------------------------------------------------------


class TestChatHistoryTenantIsolation:
    def test_a_conversation_inherits_its_owners_tenant(self, session):
        user = _user_in(session, "a@example.com", "tenant_a")
        conversation = create_conversation(session, user_id=user.id)
        assert conversation.tenant_id == "tenant_a"

    def test_one_tenants_user_cannot_read_anothers_conversation(self, session):
        owner = _user_in(session, "a@example.com", "tenant_a")
        intruder = _user_in(session, "b@example.com", "tenant_b")
        conversation = create_conversation(session, user_id=owner.id)

        assert (
            get_conversation(session, conversation_id=conversation.id, user_id=intruder.id) is None
        )

    def test_a_conversation_whose_tenant_no_longer_matches_its_owner_is_invisible(self, session):
        """The defense-in-depth property `_tenant_matches_owner` exists for:
        a row whose tenant is somehow wrong (a bad backfill, a hand-written
        UPDATE, a user reassigned between tenants) fails closed rather than
        leaking. Without that condition this read would still succeed,
        because `user_id` alone still matches."""
        owner = _user_in(session, "a@example.com", "tenant_a")
        conversation = create_conversation(session, user_id=owner.id)

        session.query(Conversation).filter_by(id=conversation.id).update({"tenant_id": "tenant_b"})
        session.commit()

        assert get_conversation(session, conversation_id=conversation.id, user_id=owner.id) is None
        rows, total = list_conversations(session, user_id=owner.id, limit=10, offset=0)
        assert rows == []
        assert total == 0

    def test_history_search_also_applies_the_tenant_condition(self, session):
        owner = _user_in(session, "a@example.com", "tenant_a")
        conversation = create_conversation(session, user_id=owner.id, title="orders report")
        session.query(Conversation).filter_by(id=conversation.id).update({"tenant_id": "tenant_b"})
        session.commit()

        hits, total = search_history(session, user_id=owner.id, query="orders", limit=10, offset=0)
        assert hits == []
        assert total == 0

    def test_creating_a_conversation_for_an_unknown_user_fails_loudly(self, session):
        with pytest.raises(ValueError):
            create_conversation(session, user_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# 3. Configuration: database connections
# ---------------------------------------------------------------------------


def _settings_with_restricted_databases() -> Settings:
    return Settings(
        db_type="postgres",
        db_host="h",
        db_name="n",
        db_user="u",
        databases=(
            DatabaseConnectionConfig(name="shared", db_type="postgres"),
            DatabaseConnectionConfig(name="a_only", db_type="postgres", tenant_ids=("tenant_a",)),
            DatabaseConnectionConfig(name="b_only", db_type="postgres", tenant_ids=("tenant_b",)),
        ),
    )


class TestDatabaseConnectionTenantIsolation:
    def test_a_tenant_sees_only_shared_plus_its_own_connections(self):
        settings = _settings_with_restricted_databases()
        assert [c.name for c in settings.databases_for_tenant("tenant_a")] == [
            "shared",
            "a_only",
        ]
        assert [c.name for c in settings.databases_for_tenant("tenant_b")] == [
            "shared",
            "b_only",
        ]

    def test_an_unresolvable_tenant_sees_only_shared_connections(self):
        """`None` must never mean "matches everything" -- deny by default."""
        settings = _settings_with_restricted_databases()
        assert [c.name for c in settings.databases_for_tenant(None)] == ["shared"]

    def test_a_connection_with_no_restriction_stays_shared(self):
        """Backward compatibility: an existing `.env` sets no tenant ids at
        all, and every tenant must keep seeing every database."""
        settings = Settings(
            db_type="postgres",
            db_host="h",
            db_name="n",
            db_user="u",
            databases=(
                DatabaseConnectionConfig(name="one", db_type="postgres"),
                DatabaseConnectionConfig(name="two", db_type="postgres"),
            ),
        )
        assert len(settings.databases_for_tenant("any_tenant")) == 2

    def test_agent_routing_never_considers_another_tenants_database(self, monkeypatch):
        """`select_database` must not even score a restricted connection --
        otherwise a question could be routed to, and have schema retrieved
        from, a database the asking tenant may not use."""
        settings = Settings(
            db_type="postgres",
            db_host="h",
            db_name="n",
            db_user="u",
            databases=(
                DatabaseConnectionConfig(
                    name="a_only", db_type="postgres", tenant_ids=("tenant_a",)
                ),
                DatabaseConnectionConfig(
                    name="b_only", db_type="postgres", tenant_ids=("tenant_b",)
                ),
            ),
        )

        def _never(*args, **kwargs):
            raise AssertionError("must not query a collection for a cross-tenant database")

        monkeypatch.setattr("embeddings.retriever.get_chroma_client", _never)

        # Exactly one visible database -> the short-circuit path, and it is
        # tenant_a's own.
        assert select_database("anything", settings, tenant_id="tenant_a").db_name == "a_only"
        assert select_database("anything", settings, tenant_id="tenant_b").db_name == "b_only"

    def test_a_tenant_with_no_usable_database_is_refused(self, monkeypatch):
        settings = Settings(
            db_type="postgres",
            db_host="h",
            db_name="n",
            db_user="u",
            databases=(
                DatabaseConnectionConfig(
                    name="a_only", db_type="postgres", tenant_ids=("tenant_a",)
                ),
            ),
        )
        with pytest.raises(SchemaRetrievalError):
            select_database("anything", settings, tenant_id="tenant_b")


# ---------------------------------------------------------------------------
# 4. Observability
# ---------------------------------------------------------------------------


class TestMetricsTenantIsolation:
    def test_one_tenants_rollup_excludes_anothers_requests(self):
        metrics = PerformanceMetrics()
        metrics.record_agent_run(
            [{"stage": "generate_sql", "attempt": 1, "duration_ms": 10.0}],
            total_duration_ms=20.0,
            status="succeeded",
            tenant_id="tenant_a",
        )
        metrics.record_agent_run(
            [{"stage": "generate_sql", "attempt": 1, "duration_ms": 900.0}],
            total_duration_ms=1000.0,
            status="failed",
            tenant_id="tenant_b",
        )

        a = metrics.snapshot("tenant_a")
        assert a["requests"]["count"] == 1
        assert a["status_counts"] == {"succeeded": 1}
        assert a["requests"]["max_ms"] == 20.0

        b = metrics.snapshot("tenant_b")
        assert b["status_counts"] == {"failed": 1}

    def test_cache_counters_are_partitioned_too(self):
        metrics = PerformanceMetrics()
        metrics.record_result_cache_hit("tenant_a")
        metrics.record_database_concurrency_rejection("tenant_a")

        assert metrics.snapshot("tenant_b")["result_cache_hits"] == 0
        assert metrics.snapshot("tenant_b")["database_concurrency_rejections"] == 0
        assert metrics.snapshot("tenant_a")["result_cache_hits"] == 1

    def test_a_tenant_that_recorded_nothing_is_indistinguishable_from_one_that_does_not_exist(
        self,
    ):
        """A metrics endpoint must not become a tenant-existence oracle."""
        metrics = PerformanceMetrics()
        metrics.record_agent_run([], total_duration_ms=1.0, status="succeeded", tenant_id="real")

        silent = metrics.snapshot("real_but_silent")
        nonexistent = metrics.snapshot("never_heard_of_it")
        assert silent["requests"] == nonexistent["requests"]
        assert silent["status_counts"] == nonexistent["status_counts"] == {}

    def test_the_merged_view_still_exists_for_operators(self):
        """`snapshot()` with no tenant keeps its pre-Prompt-20 meaning -- it
        is simply not reachable over HTTP."""
        metrics = PerformanceMetrics()
        metrics.record_agent_run([], total_duration_ms=1.0, status="succeeded", tenant_id="a")
        metrics.record_agent_run([], total_duration_ms=2.0, status="succeeded", tenant_id="b")

        assert metrics.snapshot()["requests"]["count"] == 2
        assert metrics.snapshot()["tenant_id"] is None


# ---------------------------------------------------------------------------
# 5. The four pre-existing ABAC policy modules, driven by REAL user tenants
# ---------------------------------------------------------------------------


class TestPolicyModulesAreNowLoadBearing:
    """The point of Prompt 20, asserted directly.

    These four deny-by-default policy modules already existed and already
    compared tenants -- but `resolve_actor_tenant_id` returned one hardcoded
    constant, so `actor_tenant_id` and the resource's `tenant_id` were always
    the same string and the cross-tenant branch was unreachable with real
    users. Every case below derives `actor_tenant_id` from a real
    `users.tenant_id` row via the real resolver, so these exercise the branch
    that was previously dead.

    The pre-existing suites (`tests/test_api_onboarding.py`,
    `tests/test_api_semantic_catalog.py`,
    `tests/test_api_recommendation_governance.py`,
    `tests/test_api_shares.py`) monkeypatch the resolver to simulate two
    tenants; they are still valuable and unchanged -- this is the
    no-simulation counterpart.
    """

    def test_onboarding_job_is_denied_across_tenants(self, session):
        from identity.rbac import Permission

        from onboarding.policy import OnboardingAction, authorize_onboarding_action

        intruder = _user_in(session, "b@example.com", "tenant_b")
        decision = authorize_onboarding_action(
            actor_permissions=frozenset(
                {Permission.ONBOARDING_MANAGE, Permission.ONBOARDING_REVIEW}
            ),
            actor_tenant_id=resolve_actor_tenant_id(intruder),
            job_tenant_id="tenant_a",
            action=OnboardingAction.VIEW_JOB,
        )

        assert decision.allowed is False

    def test_onboarding_job_is_allowed_within_a_tenant(self, session):
        """Pins the positive case, so the denial above can't be passing for
        an unrelated reason (e.g. a missing permission)."""
        from identity.rbac import Permission

        from onboarding.policy import OnboardingAction, authorize_onboarding_action

        owner = _user_in(session, "a@example.com", "tenant_a")
        decision = authorize_onboarding_action(
            actor_permissions=frozenset(
                {Permission.ONBOARDING_MANAGE, Permission.ONBOARDING_REVIEW}
            ),
            actor_tenant_id=resolve_actor_tenant_id(owner),
            job_tenant_id="tenant_a",
            action=OnboardingAction.VIEW_JOB,
        )

        assert decision.allowed is True

    def test_semantic_catalog_entry_is_denied_across_tenants(self, session):
        from identity.rbac import Permission
        from semantic.catalog_policy import CatalogAction, authorize_catalog_action

        intruder = _user_in(session, "b@example.com", "tenant_b")
        decision = authorize_catalog_action(
            actor_permissions=frozenset({Permission.CATALOG_MANAGE, Permission.CATALOG_REVIEW}),
            actor_tenant_id=resolve_actor_tenant_id(intruder),
            entry_tenant_id="tenant_a",
            action=CatalogAction.VIEW_ENTRY,
        )

        assert decision.allowed is False

    def test_recommendation_record_is_denied_across_tenants(self, session):
        from identity.rbac import Permission
        from recommendation.governance_policy import (
            RecommendationAction,
            authorize_recommendation_action,
        )

        intruder = _user_in(session, "b@example.com", "tenant_b")
        decision = authorize_recommendation_action(
            actor_permissions=frozenset(
                {Permission.RECOMMENDATION_REVIEW, Permission.RECOMMENDATION_MANAGE}
            ),
            actor_tenant_id=resolve_actor_tenant_id(intruder),
            record_tenant_id="tenant_a",
            action=RecommendationAction.VIEW,
        )

        assert decision.allowed is False

    def test_an_actor_with_no_resolvable_tenant_is_denied(self, session):
        """`None` must never match a resource's tenant -- deny by default."""
        from identity.rbac import Permission

        from onboarding.policy import OnboardingAction, authorize_onboarding_action

        decision = authorize_onboarding_action(
            actor_permissions=frozenset({Permission.ONBOARDING_MANAGE}),
            actor_tenant_id=resolve_actor_tenant_id(None),
            job_tenant_id="tenant_a",
            action=OnboardingAction.VIEW_JOB,
        )

        assert decision.allowed is False
