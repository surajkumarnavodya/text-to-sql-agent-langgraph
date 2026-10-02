"""End-to-end HTTP tests for api/recommendation_governance.py (Prompt 18,
`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`) -- same `TestClient` +
in-memory-SQLite-identity-DB pattern as `tests/test_api_semantic_catalog.py`.

Two users are never enough to prove tenant isolation on their own (this
app has no real multi-tenant column outside a handful of new-feature
tables -- see `security/tenancy.py`'s own docstring), so
`_fake_tenant_resolver` below simulates two tenants deterministically by
email domain, monkeypatched onto `api.recommendation_governance
.resolve_actor_tenant_id` -- this exercises the *real* ABAC tenant check
in `recommendation.governance_policy.authorize_recommendation_action`,
not a mocked stand-in for it.

There is no HTTP route to create a `RecommendationRecord` (see
`api/recommendation_governance.py`'s own docstring -- only the live
`/ask` pipeline creates one), so every test here seeds a record directly
via `identity.repositories.recommendation_governance.create_record`
against the same test database the `TestClient` requests hit.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, RecommendationRecord, User
from identity.repositories.recommendation_governance import create_record
from identity.repositories.users import assign_role
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.recommendation_governance as governance_mod
from agent.provenance import DataTruthLevel, ProvenancedClaim
from config.settings import Settings

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


def _fake_tenant_resolver(user: User | None) -> str | None:
    if user is None:
        return None
    if user.email.endswith("@tenant-a.example.com"):
        return "tenant-a"
    if user.email.endswith("@tenant-b.example.com"):
        return "tenant-b"
    return "default"


@pytest.fixture(autouse=True)
def _identity_test_db(monkeypatch) -> Iterator[object]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(governance_mod, "resolve_actor_tenant_id", _fake_tenant_resolver)

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()

    yield engine


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _register(client: TestClient, email: str, role: str | None = None) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "correcthorse battery staple 1",
            "display_name": email.split("@")[0],
        },
    )
    assert response.status_code == 201, response.text
    tokens = response.json()
    if role is not None:
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            user = session.query(User).filter_by(email=email.strip().lower()).one()
            assign_role(session, user_id=user.id, role_name=role, assigned_by_user_id=None)
            session.commit()
        finally:
            session.close()
    return tokens


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _recommendation(**overrides) -> Recommendation:
    defaults = dict(
        kind=RecommendationKind.ACTION,
        claim=ProvenancedClaim(
            value="Investigate the anomalous spike.",
            level=DataTruthLevel.AI_INFERENCE,
            source="recommendation.engine.AnomalyRule",
        ),
        category=RecommendationCategory.ANOMALY,
        evidence=(
            ProvenancedClaim(
                value="Value at 2024 flagged anomalous.",
                level=DataTruthLevel.DATABASE_FACT,
                source="analytics.engine",
            ),
        ),
        affected_entity="2024",
        action="Investigate.",
        confidence=0.8,
        rule_or_model="recommendation.engine.AnomalyRule",
    )
    defaults.update(overrides)
    return Recommendation(**defaults)


def _seed_record(tenant_id: str = "tenant-a", database_id: str = "hr", **overrides) -> str:
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        record: RecommendationRecord = create_record(
            session,
            tenant_id=tenant_id,
            database_id=database_id,
            recommendation=_recommendation(**overrides),
            created_by_user_id=None,
            source_question="why did sales spike?",
            source_sql="SELECT 1",
        )
        return str(record.id)
    finally:
        session.close()


class TestListAndGetRbac:
    def test_admin_can_list(self, client: TestClient):
        _seed_record()
        tokens = _register(client, "admin@tenant-a.example.com", role="admin")
        response = client.get("/recommendations", headers=_headers(tokens))
        assert response.status_code == 200, response.text
        assert len(response.json()) == 1

    def test_analyst_can_list(self, client: TestClient):
        _seed_record()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        response = client.get("/recommendations", headers=_headers(tokens))
        assert response.status_code == 200, response.text

    def test_plain_user_is_forbidden(self, client: TestClient):
        _seed_record()
        tokens = _register(client, "plain@tenant-a.example.com")
        response = client.get("/recommendations", headers=_headers(tokens))
        assert response.status_code == 403

    def test_get_single_record(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "admin2@tenant-a.example.com", role="admin")
        response = client.get(f"/recommendations/{record_id}", headers=_headers(tokens))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "generated"
        assert body["category"] == "anomaly"
        assert body["evidence"]

    def test_get_events_returns_the_initial_generated_event(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "admin3@tenant-a.example.com", role="admin")
        response = client.get(f"/recommendations/{record_id}/events", headers=_headers(tokens))
        assert response.status_code == 200, response.text
        events = response.json()
        assert len(events) == 1
        assert events[0]["from_status"] is None
        assert events[0]["to_status"] == "generated"


class TestTenantIsolation:
    def test_a_record_in_one_tenant_is_invisible_to_an_admin_in_another(self, client: TestClient):
        record_id = _seed_record(tenant_id="tenant-a")
        other_tokens = _register(client, "other@tenant-b.example.com", role="admin")
        response = client.get(f"/recommendations/{record_id}", headers=_headers(other_tokens))
        assert response.status_code == 404

    def test_a_random_record_id_is_also_a_404(self, client: TestClient):
        tokens = _register(client, "admin4@tenant-a.example.com", role="admin")
        response = client.get(f"/recommendations/{uuid.uuid4()}", headers=_headers(tokens))
        assert response.status_code == 404

    def test_cross_tenant_feedback_is_denied(self, client: TestClient):
        record_id = _seed_record(tenant_id="tenant-a")
        other_tokens = _register(client, "other2@tenant-b.example.com", role="admin")
        response = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "accepted"},
            headers=_headers(other_tokens),
        )
        assert response.status_code == 404

    def test_list_only_returns_the_callers_own_tenant(self, client: TestClient):
        _seed_record(tenant_id="tenant-a")
        _seed_record(tenant_id="tenant-b")
        tokens = _register(client, "admin5@tenant-a.example.com", role="admin")
        response = client.get("/recommendations", headers=_headers(tokens))
        assert response.status_code == 200
        assert len(response.json()) == 1


class TestFeedbackLifecycle:
    def test_analyst_can_submit_a_verdict(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "analyst2@tenant-a.example.com", role="analyst")
        response = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "accepted", "reason": "Confirmed useful."},
            headers=_headers(tokens),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"

    def test_plain_user_cannot_submit_feedback(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "plain2@tenant-a.example.com")
        response = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "accepted"},
            headers=_headers(tokens),
        )
        assert response.status_code == 403

    def test_feedback_cannot_target_resolved_or_expired(self, client: TestClient):
        """`resolved`/`expired` are only reachable through their own
        dedicated routes -- the general feedback endpoint's own request
        schema rejects them outright (422, a validation error, never
        reaching the repository at all)."""
        record_id = _seed_record()
        tokens = _register(client, "analyst3@tenant-a.example.com", role="analyst")
        for target in ("resolved", "expired"):
            response = client.post(
                f"/recommendations/{record_id}/feedback",
                json={"status": target},
                headers=_headers(tokens),
            )
            assert response.status_code == 422

    def test_invalid_transition_is_a_409(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "analyst4@tenant-a.example.com", role="analyst")
        # accepted -> accepted is fine first, then try resolved -> accepted
        # is invalid (resolved has no outgoing transitions at all).
        client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "accepted"},
            headers=_headers(tokens),
        )
        client.post(f"/recommendations/{record_id}/resolve", json={}, headers=_headers(tokens))
        response = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "rejected"},
            headers=_headers(tokens),
        )
        assert response.status_code == 409

    def test_resolve_requires_an_accepted_or_partially_useful_record(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "analyst5@tenant-a.example.com", role="analyst")
        response = client.post(
            f"/recommendations/{record_id}/resolve", json={}, headers=_headers(tokens)
        )
        assert response.status_code == 409

    def test_full_lifecycle_generated_accepted_resolved(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "analyst6@tenant-a.example.com", role="analyst")
        headers = _headers(tokens)

        accept = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "accepted", "reason": "Good catch."},
            headers=headers,
        )
        assert accept.json()["status"] == "accepted"

        resolve = client.post(
            f"/recommendations/{record_id}/resolve",
            json={"reason": "Root cause fixed."},
            headers=headers,
        )
        assert resolve.json()["status"] == "resolved"

        events = client.get(f"/recommendations/{record_id}/events", headers=headers).json()
        assert [e["to_status"] for e in events] == ["generated", "accepted", "resolved"]
        assert events[1]["reason"] == "Good catch."
        assert events[2]["reason"] == "Root cause fixed."


class TestExpireIsAdminOnly:
    def test_analyst_cannot_expire(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "analyst7@tenant-a.example.com", role="analyst")
        response = client.post(
            f"/recommendations/{record_id}/expire", json={}, headers=_headers(tokens)
        )
        assert response.status_code == 403

    def test_admin_can_expire(self, client: TestClient):
        record_id = _seed_record()
        tokens = _register(client, "admin6@tenant-a.example.com", role="admin")
        response = client.post(
            f"/recommendations/{record_id}/expire",
            json={"reason": "Superseded by a fresher run."},
            headers=_headers(tokens),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "expired"


class TestQualityMetrics:
    def test_metrics_reflect_feedback(self, client: TestClient):
        id_a = _seed_record()
        id_b = _seed_record()
        tokens = _register(client, "admin7@tenant-a.example.com", role="admin")
        headers = _headers(tokens)
        client.post(
            f"/recommendations/{id_a}/feedback", json={"status": "accepted"}, headers=headers
        )
        client.post(
            f"/recommendations/{id_b}/feedback", json={"status": "rejected"}, headers=headers
        )

        response = client.get("/recommendations/metrics", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert body["judged_total"] == 2
        assert body["acceptance_rate"] == pytest.approx(0.5)

    def test_plain_user_cannot_read_metrics(self, client: TestClient):
        tokens = _register(client, "plain3@tenant-a.example.com")
        response = client.get("/recommendations/metrics", headers=_headers(tokens))
        assert response.status_code == 403

    def test_metrics_route_is_not_swallowed_by_the_record_id_path(self, client: TestClient):
        """A literal `/recommendations/metrics` must never be matched by
        the `/{record_id}` route (which would 422 on a non-UUID path
        segment) -- registration order in api/recommendation_governance.py
        guards this."""
        tokens = _register(client, "admin8@tenant-a.example.com", role="admin")
        response = client.get("/recommendations/metrics", headers=_headers(tokens))
        assert response.status_code == 200
