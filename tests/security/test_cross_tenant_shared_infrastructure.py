"""Cross-tenant negative tests for the *shared* parts of the stack --
Prompt 20 (`20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`).

Companion to `tests/security/test_cross_tenant_isolation.py`, which covers
tenant resolution, persistence, connection config and metrics. This file
covers the three things that contract names which are *structurally* shared
between tenants and therefore the hardest to reason about:

1.  **Shared model infrastructure** (`agent.llm_client`'s process-wide cached
    `ollama.Client`, `agent.graph.build_graph`'s process-wide compiled
    LangGraph graph). Both are singletons by design, so the risk is a naive
    implementation stashing the tenant somewhere global -- which only shows
    up under real concurrency. Modelled directly on
    `tests/test_model_selection_concurrency.py`, which guards the identical
    bug class for `selected_model`, including its artificial delay to force
    the race window open.
2.  **Schema metadata** (`GET /schema/tables`) -- table and column names are
    configuration a tenant must not learn about another tenant's database.
3.  **Recommendations/analytics** -- a recommendation is derived from a
    performance snapshot, so a shared snapshot would both produce wrong
    advice and leak another tenant's operational data.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from agent.graph import build_graph, run_agent
from agent.state import AgentState
from config.settings import DatabaseConnectionConfig, Settings
from observability.metrics import PerformanceMetrics
from security.audit_log import get_audit_tenant_id

# ---------------------------------------------------------------------------
# 1. Shared model infrastructure must not carry tenant context
# ---------------------------------------------------------------------------


class TestSharedGraphCannotLeakTenantContext:
    def test_the_compiled_graph_is_shared_but_holds_no_tenant(self):
        """`build_graph` is `@lru_cache`d -- one compiled graph serves every
        tenant. That is only safe because all per-question state lives in the
        `initial_state` dict passed to `.invoke()`, never on the graph."""
        first = build_graph()
        second = build_graph()
        assert first is second
        assert "tenant" not in repr(type(first)).lower()

    def test_two_concurrent_runs_never_see_each_others_tenant(self, monkeypatch):
        """The real bug class: a naive implementation could have stashed the
        tenant on the cached `Settings` singleton, a module global, or the
        compiled graph. A sequential two-call test would pass even then --
        there is no window for the second call to clobber the first's
        in-flight value. These threads plus the delay inside the fake node
        force that window open.
        """
        observed: list[tuple[str | None, str | None]] = []
        observed_lock = threading.Lock()

        def _fake_sanitize(state: AgentState) -> dict:
            # Read the tenant, yield the GIL, then record it. If the tenant
            # were read from shared mutable state at *use* time rather than
            # from this call's own state dict, the other thread's concurrent
            # write would have a real chance to land in this window.
            tenant = state.get("tenant_id")
            audit_tenant = get_audit_tenant_id()
            time.sleep(0.05)
            with observed_lock:
                observed.append((tenant, audit_tenant))
            return {"status": "rejected", "rejection_reason": "stopping the run here"}

        monkeypatch.setattr("agent.graph.sanitize_input_node", _fake_sanitize)
        build_graph.cache_clear()

        def _run(tenant: str) -> None:
            run_agent("how many orders?", tenant_id=tenant)

        threads = [
            threading.Thread(target=_run, args=("tenant_a",)),
            threading.Thread(target=_run, args=("tenant_b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(observed) == 2, observed
        # Each run saw its own tenant, in `AgentState` and in the audit
        # context alike -- never the other thread's.
        assert {pair[0] for pair in observed} == {"tenant_a", "tenant_b"}
        assert all(state_tenant == audit_tenant for state_tenant, audit_tenant in observed)

    def test_the_audit_tenant_is_unbound_again_once_a_run_finishes(self, monkeypatch):
        """Bound in a `try`/`finally`, so a tenant can never leak into
        whatever reuses this thread next -- the exact failure mode
        `set_correlation_id`'s own docstring warns about."""

        def _fake_sanitize(state: AgentState) -> dict:
            assert get_audit_tenant_id() == "tenant_a"
            return {"status": "rejected", "rejection_reason": "stopping the run here"}

        monkeypatch.setattr("agent.graph.sanitize_input_node", _fake_sanitize)
        build_graph.cache_clear()

        run_agent("anything", tenant_id="tenant_a")

        assert get_audit_tenant_id() is None

    def test_the_audit_tenant_is_unbound_even_when_a_run_raises(self, monkeypatch):
        def _boom(state: AgentState) -> dict:
            raise RuntimeError("node exploded")

        monkeypatch.setattr("agent.graph.sanitize_input_node", _boom)
        build_graph.cache_clear()

        with pytest.raises(RuntimeError, match="node exploded"):
            run_agent("anything", tenant_id="tenant_a")

        assert get_audit_tenant_id() is None


# ---------------------------------------------------------------------------
# 2. Schema metadata
# ---------------------------------------------------------------------------


#: Two tenant-restricted connections, so "which databases does this caller
#: see" has a different answer per tenant.
_RESTRICTED_SETTINGS = Settings(
    db_type="postgres",
    db_host="h",
    db_name="n",
    db_user="u",
    databases=(
        DatabaseConnectionConfig(name="a_only", db_type="postgres", tenant_ids=("tenant_a",)),
        DatabaseConnectionConfig(name="b_only", db_type="postgres", tenant_ids=("tenant_b",)),
    ),
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


@pytest.fixture
def as_tenant_b(monkeypatch):
    """Runs every request as a caller in `tenant_b`, against
    `_RESTRICTED_SETTINGS`.

    `resolve_tenant_id_for_identity` is patched at `api.main`'s own import
    site -- the same seam `tests/test_api_shares.py` uses for
    `resolve_actor_tenant_id`, and for the same reason: the real resolver's
    own behavior is covered directly in `tests/test_tenancy.py` and
    `tests/security/test_cross_tenant_isolation.py`, so what matters *here*
    is the route's behavior given a tenant, not how the tenant was derived.
    Authentication itself stays in `auth_mode="none"` (no `api_auth_token`),
    exactly as `tests/test_api_ask.py` does.
    """
    monkeypatch.setattr(api_main, "get_settings", lambda: _RESTRICTED_SETTINGS)
    monkeypatch.setattr("api.auth.get_settings", lambda: _RESTRICTED_SETTINGS)
    monkeypatch.setattr(api_main, "resolve_tenant_id_for_identity", lambda identity: "tenant_b")
    monkeypatch.setattr("api.main.get_read_only_engine", lambda settings: object())
    monkeypatch.setattr("api.main.introspect_schema", lambda engine, schema: [])


class TestSchemaMetadataTenantIsolation:
    def test_a_tenant_cannot_list_another_tenants_database(self, client, as_tenant_b):
        """Asking for a database bound to another tenant must read as "no such
        database", never "exists but forbidden" -- otherwise the route is a
        tenant/database existence oracle."""
        response = client.get("/schema/tables?database=a_only")

        assert response.status_code == 404, response.text
        detail = response.json()["detail"]
        assert "a_only" in detail
        assert "forbid" not in detail.lower()
        assert "tenant" not in detail.lower()

    def test_the_unfiltered_listing_omits_another_tenants_database(
        self, client, as_tenant_b, monkeypatch
    ):
        """Omitting `?database=` must not fall back to listing *every*
        configured database -- that would hand a tenant the full inventory in
        one unauthenticated-looking call.

        Asserted on which connections were actually *introspected*, not just
        on the response body: a route that queried `a_only` and then filtered
        it out of the response would already have leaked it to the logs and
        burned a connection against another tenant's database.
        """
        introspected: list[str] = []

        def _engine_for(config):
            introspected.append(config.name)
            return object()

        monkeypatch.setattr("api.main.get_read_only_engine", _engine_for)

        response = client.get("/schema/tables")

        assert response.status_code == 200, response.text
        assert response.json()["tables"] == []
        assert introspected == ["b_only"]

    def test_a_tenants_own_database_is_still_reachable(self, client, as_tenant_b):
        """The negative tests above would also pass if the route were simply
        broken, so this pins the positive case."""
        response = client.get("/schema/tables?database=b_only")

        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 3. Recommendations / analytics
# ---------------------------------------------------------------------------


class TestRecommendationInputsAreTenantScoped:
    def test_a_recommendation_never_draws_on_another_tenants_performance_data(self):
        """`generate_recommendations_node` feeds
        `PerformanceMetrics.snapshot(state["tenant_id"])` into the engine. A
        process-wide snapshot would have produced advice derived from another
        tenant's latency -- both wrong and a leak of their operational data.
        """
        metrics = PerformanceMetrics()
        # Tenant B is pathologically slow; tenant A is fast.
        for _ in range(5):
            metrics.record_agent_run(
                [{"stage": "generate_sql", "attempt": 1, "duration_ms": 60_000.0}],
                total_duration_ms=61_000.0,
                status="succeeded",
                tenant_id="tenant_b",
            )
        metrics.record_agent_run(
            [{"stage": "generate_sql", "attempt": 1, "duration_ms": 12.0}],
            total_duration_ms=20.0,
            status="succeeded",
            tenant_id="tenant_a",
        )

        a_snapshot = metrics.snapshot("tenant_a")
        b_snapshot = metrics.snapshot("tenant_b")

        # Tenant A's own view must show only its own single fast request --
        # no trace of tenant B's five slow ones.
        assert a_snapshot["requests"]["count"] == 1
        assert a_snapshot["requests"]["max_ms"] == 20.0
        assert a_snapshot["requests"]["p95_ms"] < 1_000.0
        assert b_snapshot["requests"]["p95_ms"] > 1_000.0

    def test_analytics_inputs_carry_no_cross_request_state(self):
        """`analytics.engine.compute_analytics_result` is a pure function over
        one result set -- there is no cross-request store for a tenant to leak
        through. Asserted structurally rather than taken on trust."""
        import inspect

        from analytics import engine

        source = inspect.getsource(engine)
        # No module-level mutable cache/singleton that could outlive a request.
        assert "global " not in source
        for forbidden in ("lru_cache", "functools.cache", "@cache"):
            assert forbidden not in source, f"{forbidden} would introduce cross-request state"
