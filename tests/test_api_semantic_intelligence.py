"""HTTP tests for `api/semantic_intelligence.py` (Prompt 35). Same in-memory
identity-database and real-user pattern as `tests/test_api_semantic_catalog.py`.
Tenants are simulated by email domain, monkeypatched onto this module's own
`resolve_actor_tenant_id`, so the real ABAC check in
`semantic.catalog_policy.authorize_catalog_action` is what runs."""

from __future__ import annotations

from pathlib import Path

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, User
from identity.repositories.semantic_catalog import create_entry
from identity.repositories.users import assign_role
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.semantic_catalog as catalog_mod
import api.semantic_intelligence as si_mod
from config.settings import Settings
from security.secrets import SecretStr

_BASE = Settings(
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
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    schema_top_k=4,
    max_retries=3,
    complex_query_max_retry_bonus=2,
    max_result_rows=1000,
    query_timeout_seconds=15,
    llm_max_tokens=1024,
    insight_max_tokens=120,
    max_question_length=500,
    question_rate_limit_per_minute=10,
    llm_call_rate_limit_per_minute=20,
    cost_estimation_enabled=False,
    log_level="INFO",
    log_redaction_level="standard",
    enable_semantic_intelligence=True,
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE.__dict__, **overrides})


def _fake_tenant_resolver(user: User | None) -> str | None:
    if user is None:
        return None
    if user.email.endswith("@tenant-a.example.com"):
        return "tenant-a"
    if user.email.endswith("@tenant-b.example.com"):
        return "tenant-b"
    return "default"


@pytest.fixture(autouse=True)
def _identity_test_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _BASE)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _BASE)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _BASE)
    monkeypatch.setattr(si_mod, "get_settings", lambda: _settings())
    monkeypatch.setattr(si_mod, "resolve_actor_tenant_id", _fake_tenant_resolver)
    # The catalog's own routes resolve tenants separately; both must agree.
    monkeypatch.setattr(catalog_mod, "resolve_actor_tenant_id", _fake_tenant_resolver)
    session = identity_db_mod.get_identity_session(_BASE)
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
        session = identity_db_mod.get_identity_session(_BASE)
        try:
            user = session.query(User).filter_by(email=email.strip().lower()).one()
            assign_role(session, user_id=user.id, role_name=role, assigned_by_user_id=None)
            session.commit()
        finally:
            session.close()
    return tokens


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _seed_conflicting_metrics(tenant: str = "tenant-a") -> None:
    """Two metrics sharing the term 'Revenue' with different formulas, in the
    default database of one tenant. Created directly through the repository,
    the same path the catalog API uses."""
    session = identity_db_mod.get_identity_session(_BASE)
    try:
        for key, expression in (("revenue_gross", "SUM(gross)"), ("revenue_net", "SUM(net)")):
            create_entry(
                session,
                tenant_id=tenant,
                database_id="default",
                concept_type="metric",
                concept_key=key,
                business_name="Revenue",
                technical_name=None,
                description="",
                grain=None,
                keys=[],
                relationships=[],
                domain=None,
                synonyms=[],
                business_rules=[],
                examples=[],
                evidence=[],
                confidence=1.0,
                owner=None,
                created_by_user_id=None,
                approved_expression=expression,
                source_tables=["sales"],
                filters=[],
                dimensions=[],
                aggregation="sum",
            )
    finally:
        session.close()


class TestFeatureFlagAndAuthorization:
    def test_every_route_is_hidden_when_the_flag_is_off(self, monkeypatch, client):
        monkeypatch.setattr(
            si_mod, "get_settings", lambda: _settings(enable_semantic_intelligence=False)
        )
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        response = client.post(
            "/semantic-intelligence/run?database_id=default", headers=_headers(tokens)
        )

        assert response.status_code == 404

    def test_a_viewer_without_catalog_review_cannot_run_the_analysis(self, client):
        tokens = _register(client, "viewer@tenant-a.example.com", role="viewer")

        response = client.post(
            "/semantic-intelligence/run?database_id=default", headers=_headers(tokens)
        )

        assert response.status_code == 403

    def test_an_unknown_database_is_a_not_found_not_a_forbidden(self, client):
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        response = client.post(
            "/semantic-intelligence/run?database_id=other-tenants-db", headers=_headers(tokens)
        )

        assert response.status_code == 404

    def test_a_decision_body_with_an_unknown_field_is_rejected(self, client):
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        response = client.post(
            "/semantic-intelligence/findings/00000000-0000-0000-0000-000000000000/decision",
            headers=_headers(tokens),
            json={"decision": "accept", "tenant_id": "tenant-b"},
        )

        assert response.status_code == 422

    def test_a_decision_must_be_accept_or_dismiss(self, client):
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        response = client.post(
            "/semantic-intelligence/findings/00000000-0000-0000-0000-000000000000/decision",
            headers=_headers(tokens),
            json={"decision": "publish"},
        )

        assert response.status_code == 422


class TestAnalysisAndQueue:
    def test_a_run_detects_the_conflict_and_reports_it_as_an_inference(self, client):
        _seed_conflicting_metrics()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        body = client.post(
            "/semantic-intelligence/run?database_id=default", headers=_headers(tokens)
        ).json()

        assert body["entries_analysed"] == 2
        conflicts = [f for f in body["top_findings"] if f["kind"] == "conflict"]
        assert conflicts and all(f["truth_level"] == "ai_inference" for f in conflicts)

    def test_the_queue_is_ordered_by_risk(self, client):
        _seed_conflicting_metrics()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(tokens))

        queue = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(tokens)
        ).json()

        scores = [f["risk_score"] for f in queue]
        assert scores == sorted(scores, reverse=True)

    def test_rerunning_on_unchanged_data_changes_nothing(self, client):
        _seed_conflicting_metrics()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(tokens))
        second = client.post(
            "/semantic-intelligence/run?database_id=default", headers=_headers(tokens)
        ).json()

        assert second["created"] == 0 and second["updated"] == 0 and second["unchanged"] > 0

    def test_accepting_then_deciding_again_is_a_conflict(self, client):
        _seed_conflicting_metrics()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(tokens))
        finding = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(tokens)
        ).json()[0]

        first = client.post(
            f"/semantic-intelligence/findings/{finding['id']}/decision",
            headers=_headers(tokens),
            json={"decision": "accept", "note": "reviewed"},
        )
        second = client.post(
            f"/semantic-intelligence/findings/{finding['id']}/decision",
            headers=_headers(tokens),
            json={"decision": "dismiss"},
        )

        assert first.status_code == 200 and first.json()["status"] == "accepted"
        assert second.status_code == 409

    def test_a_finding_from_another_tenant_is_a_not_found(self, client):
        _seed_conflicting_metrics(tenant="tenant-a")
        owner = _register(client, "owner@tenant-a.example.com", role="analyst")
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(owner))
        finding = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(owner)
        ).json()[0]

        other = _register(client, "other@tenant-b.example.com", role="analyst")
        response = client.post(
            f"/semantic-intelligence/findings/{finding['id']}/decision",
            headers=_headers(other),
            json={"decision": "accept"},
        )

        assert response.status_code == 404
        listing = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(other)
        ).json()
        assert listing == []


class TestImpactAndRollback:
    def test_impact_lists_the_metrics_that_share_a_source(self, client):
        _seed_conflicting_metrics()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        body = client.get(
            "/semantic-intelligence/impact/revenue_gross?database_id=default",
            headers=_headers(tokens),
        ).json()

        assert body["concept_key"] == "revenue_gross"
        assert {d["concept_key"] for d in body["dependents"]} == {"revenue_net"}

    def test_impact_on_an_unknown_concept_is_a_not_found(self, client):
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        response = client.get(
            "/semantic-intelligence/impact/missing?database_id=default", headers=_headers(tokens)
        )

        assert response.status_code == 404

    def test_a_finding_rollback_restores_an_earlier_version(self, client):
        _seed_conflicting_metrics()
        admin = _register(client, "admin@tenant-a.example.com", role="admin")
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(admin))
        finding = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(admin)
        ).json()[0]
        assert finding["version"] == 1

        # Change the underlying definition, then rerun: the finding gains version 2.
        session = identity_db_mod.get_identity_session(_BASE)
        try:
            from identity.models import SemanticCatalogEntry as Entry

            row = session.query(Entry).filter_by(concept_key="revenue_net").one()
            # A change the finding actually records (its differing fields), so the
            # analysis produces a genuinely new version.
            row.source_tables = ["returns"]
            session.commit()
        finally:
            session.close()
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(admin))

        restored = client.post(
            f"/semantic-intelligence/findings/{finding['id']}/rollback",
            headers=_headers(admin),
            json={"to_version": 1},
        )

        assert restored.status_code == 200
        assert restored.json()["version"] == 3
        assert restored.json()["status"] == "open"

    def test_a_finding_rollback_needs_manage_permission(self, client):
        _seed_conflicting_metrics()
        admin = _register(client, "admin@tenant-a.example.com", role="admin")
        client.post("/semantic-intelligence/run?database_id=default", headers=_headers(admin))
        finding = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(admin)
        ).json()[0]
        analyst = _register(client, "analyst@tenant-a.example.com", role="analyst")

        response = client.post(
            f"/semantic-intelligence/findings/{finding['id']}/rollback",
            headers=_headers(analyst),
            json={"to_version": 1},
        )

        assert response.status_code == 403

    def test_an_entry_rollback_needs_manage_permission_not_just_review(self, client):
        _seed_conflicting_metrics()
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        listing = client.get("/semantic-catalog/entries", headers=_headers(tokens))
        assert listing.status_code == 200
        entry_id = listing.json()[0]["id"]

        response = client.post(
            f"/semantic-intelligence/entries/{entry_id}/rollback",
            headers=_headers(tokens),
            json={"target_version": 1},
        )

        assert response.status_code == 403

    def test_an_admin_can_roll_a_catalog_entry_back_to_an_earlier_version(self, client):
        _seed_conflicting_metrics()
        admin = _register(client, "admin@tenant-a.example.com", role="admin")
        listing = client.get("/semantic-catalog/entries", headers=_headers(admin)).json()
        entry_id = listing[0]["id"]

        response = client.post(
            f"/semantic-intelligence/entries/{entry_id}/rollback",
            headers=_headers(admin),
            json={"target_version": 1},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "draft" and response.json()["version"] == 2


class TestCapAndTenantGuard:
    def test_findings_beyond_the_persistence_cap_are_reported_not_silently_dropped(
        self, monkeypatch, client
    ):
        _seed_conflicting_metrics()
        monkeypatch.setattr(
            si_mod, "get_settings", lambda: _settings(semantic_intelligence_max_findings=1)
        )
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        body = client.post(
            "/semantic-intelligence/run?database_id=default", headers=_headers(tokens)
        ).json()

        assert body["created"] == 1
        assert body["findings_truncated"] == body["findings_detected"] - 1 >= 0

    def test_a_user_with_no_resolvable_tenant_is_refused_not_queried_with_none(
        self, monkeypatch, client
    ):
        monkeypatch.setattr(si_mod, "resolve_actor_tenant_id", lambda user: None)
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")

        response = client.get(
            "/semantic-intelligence/findings?database_id=default", headers=_headers(tokens)
        )

        assert response.status_code == 403
