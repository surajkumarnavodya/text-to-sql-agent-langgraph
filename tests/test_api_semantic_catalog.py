"""End-to-end HTTP tests for api/semantic_catalog.py (Prompt 09,
`09_SEMANTIC_CATALOG_CONTRACT.md`) -- same `TestClient` +
in-memory-SQLite-identity-DB pattern as `tests/test_api_onboarding.py`.

Two users are never enough to prove tenant isolation on their own (this
app has no real multi-tenant column outside a handful of new-feature
tables -- see `security/tenancy.py`'s own docstring), so
`_fake_tenant_resolver` below simulates two tenants deterministically by
email domain, monkeypatched onto `api.semantic_catalog.resolve_actor_tenant_id`
-- this exercises the *real* ABAC tenant check in
`semantic.catalog_policy.authorize_catalog_action`, not a mocked
stand-in for it.

The publish route's vector-store sync is pointed at
`tests._retrieval_fakes.InMemoryVectorStore` + `retrieval.embeddings
.FakeEmbeddingProvider` (monkeypatched onto `retrieval.ingestion`'s own
factory functions) -- this file tests the semantic-catalog REST surface's
own authorization/state-machine/retrieval-integration wiring, not a real
ChromaDB/embedding runtime.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import identity.db as identity_db_mod
import pytest
import retrieval.ingestion as ingestion_mod
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, User
from identity.repositories.users import assign_role
from retrieval.embeddings import FakeEmbeddingProvider
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.semantic_catalog as catalog_mod
from config.settings import Settings
from tests._retrieval_fakes import InMemoryVectorStore

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
def _identity_test_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(catalog_mod, "resolve_actor_tenant_id", _fake_tenant_resolver)

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()

    yield engine


@pytest.fixture(autouse=True)
def _retrieval_fakes(monkeypatch) -> Iterator[InMemoryVectorStore]:
    store = InMemoryVectorStore()
    monkeypatch.setattr(ingestion_mod, "get_vector_store", lambda settings: store)
    monkeypatch.setattr(
        ingestion_mod, "get_embedding_provider", lambda settings: FakeEmbeddingProvider()
    )
    yield store


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


def _create_entry_payload(**overrides) -> dict:
    payload = {
        "database_id": "db1",
        "concept_type": "metric",
        "concept_key": "clv",
        "business_name": "Customer Lifetime Value",
        "technical_name": "SUM(Amount)",
        "description": "Total historical revenue per customer",
        "grain": "one row per customer",
        "keys": ["CustomerId"],
        "domain": "Sales",
        "synonyms": ["CLV"],
        "owner": "alice",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def _create_entry(client: TestClient, headers: dict, **overrides) -> dict:
    response = client.post(
        "/semantic-catalog/entries", json=_create_entry_payload(**overrides), headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestCreateEntryRbac:
    def test_admin_can_create_an_entry(self, client: TestClient):
        tokens = _register(client, "admin@tenant-a.example.com", role="admin")
        entry = _create_entry(client, _headers(tokens))
        assert entry["status"] == "draft"
        assert entry["version"] == 1
        assert entry["truth_level"] == "ai_inference"

    def test_plain_user_without_catalog_manage_is_forbidden(self, client: TestClient):
        tokens = _register(client, "plain@tenant-a.example.com")
        response = client.post(
            "/semantic-catalog/entries",
            json=_create_entry_payload(),
            headers=_headers(tokens),
        )
        assert response.status_code == 403

    def test_analyst_alone_cannot_create_an_entry(self, client: TestClient):
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        response = client.post(
            "/semantic-catalog/entries",
            json=_create_entry_payload(),
            headers=_headers(tokens),
        )
        assert response.status_code == 403


class TestTenantIsolation:
    def test_an_entry_created_in_one_tenant_is_invisible_to_an_admin_in_another(
        self, client: TestClient
    ):
        owner_tokens = _register(client, "owner@tenant-a.example.com", role="admin")
        entry = _create_entry(client, _headers(owner_tokens))

        other_tenant_tokens = _register(client, "other@tenant-b.example.com", role="admin")
        response = client.get(
            f"/semantic-catalog/entries/{entry['id']}", headers=_headers(other_tenant_tokens)
        )
        assert response.status_code == 404

    def test_a_random_entry_id_is_also_a_404_not_a_403(self, client: TestClient):
        tokens = _register(client, "admin2@tenant-a.example.com", role="admin")
        response = client.get(f"/semantic-catalog/entries/{uuid.uuid4()}", headers=_headers(tokens))
        assert response.status_code == 404


class TestFullLifecycle:
    def test_create_review_publish_reaches_retrieval(
        self, client: TestClient, _retrieval_fakes: InMemoryVectorStore
    ):
        admin_tokens = _register(client, "admin3@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        analyst_tokens = _register(client, "analyst2@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)

        entry = _create_entry(client, admin_headers)
        entry_id = entry["id"]

        # A draft/reviewed entry must never already be in retrieval.
        assert _retrieval_fakes.count("db1") == 0

        review_response = client.post(
            f"/semantic-catalog/entries/{entry_id}/review",
            json={"notes": "looks good"},
            headers=analyst_headers,
        )
        assert review_response.status_code == 200, review_response.text
        assert review_response.json()["status"] == "reviewed"
        assert review_response.json()["review_notes"] == "looks good"

        # Analyst (CATALOG_REVIEW only) cannot publish.
        forbidden_publish = client.post(
            f"/semantic-catalog/entries/{entry_id}/publish", headers=analyst_headers
        )
        assert forbidden_publish.status_code == 403

        publish_response = client.post(
            f"/semantic-catalog/entries/{entry_id}/publish", headers=admin_headers
        )
        assert publish_response.status_code == 200, publish_response.text
        published = publish_response.json()
        assert published["status"] == "published"
        assert published["truth_level"] == "confirmed_business_truth"

        # Now, and only now, it's actually in the vector store.
        assert _retrieval_fakes.count("db1") == 1

    def test_request_changes_sends_a_reviewed_entry_back_to_draft(self, client: TestClient):
        admin_tokens = _register(client, "admin4@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        analyst_tokens = _register(client, "analyst3@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)

        entry = _create_entry(client, admin_headers)
        client.post(
            f"/semantic-catalog/entries/{entry['id']}/review", json={}, headers=analyst_headers
        )

        response = client.post(
            f"/semantic-catalog/entries/{entry['id']}/request-changes",
            json={"notes": "fix the grain"},
            headers=analyst_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "draft"
        assert response.json()["review_notes"] == "fix the grain"

        # Now editable again.
        edit_response = client.patch(
            f"/semantic-catalog/entries/{entry['id']}",
            json={"grain": "one row per customer per month"},
            headers=admin_headers,
        )
        assert edit_response.status_code == 200, edit_response.text
        assert edit_response.json()["grain"] == "one row per customer per month"

    def test_publishing_a_still_draft_entry_is_rejected(self, client: TestClient):
        admin_tokens = _register(client, "admin5@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        entry = _create_entry(client, admin_headers)

        response = client.post(
            f"/semantic-catalog/entries/{entry['id']}/publish", headers=admin_headers
        )
        assert response.status_code == 409

    def test_reviewing_an_already_reviewed_entry_is_rejected(self, client: TestClient):
        admin_tokens = _register(client, "admin6@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        analyst_tokens = _register(client, "analyst4@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)

        entry = _create_entry(client, admin_headers)
        client.post(
            f"/semantic-catalog/entries/{entry['id']}/review", json={}, headers=analyst_headers
        )

        response = client.post(
            f"/semantic-catalog/entries/{entry['id']}/review", json={}, headers=analyst_headers
        )
        assert response.status_code == 409

    def test_editing_a_reviewed_entry_directly_is_rejected(self, client: TestClient):
        admin_tokens = _register(client, "admin7@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        analyst_tokens = _register(client, "analyst5@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)

        entry = _create_entry(client, admin_headers)
        client.post(
            f"/semantic-catalog/entries/{entry['id']}/review", json={}, headers=analyst_headers
        )

        response = client.patch(
            f"/semantic-catalog/entries/{entry['id']}",
            json={"business_name": "Nope"},
            headers=admin_headers,
        )
        assert response.status_code == 409


class TestVersioningAndSupersession:
    def test_publishing_a_new_version_supersedes_the_prior_one_in_retrieval(
        self, client: TestClient, _retrieval_fakes: InMemoryVectorStore
    ):
        admin_tokens = _register(client, "admin8@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        analyst_tokens = _register(client, "analyst6@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)

        v1 = _create_entry(client, admin_headers)
        client.post(
            f"/semantic-catalog/entries/{v1['id']}/review", json={}, headers=analyst_headers
        )
        client.post(f"/semantic-catalog/entries/{v1['id']}/publish", headers=admin_headers)
        assert _retrieval_fakes.count("db1") == 1

        v2 = _create_entry(client, admin_headers, business_name="Customer Lifetime Value v2")
        assert v2["version"] == 2
        client.post(
            f"/semantic-catalog/entries/{v2['id']}/review", json={}, headers=analyst_headers
        )
        publish_v2 = client.post(
            f"/semantic-catalog/entries/{v2['id']}/publish", headers=admin_headers
        )
        assert publish_v2.status_code == 200, publish_v2.text
        assert publish_v2.json()["supersedes_id"] == v1["id"]

        # Still exactly one chunk in retrieval: v1's removed, v2's added.
        assert _retrieval_fakes.count("db1") == 1

        versions_response = client.get(
            "/semantic-catalog/entries/concept/clv/versions",
            params={"database_id": "db1", "concept_type": "metric"},
            headers=admin_headers,
        )
        assert versions_response.status_code == 200
        versions = versions_response.json()
        assert [v["version"] for v in versions] == [2, 1]
        assert [v["status"] for v in versions] == ["published", "superseded"]


class TestListEntries:
    def test_list_filters_by_status(self, client: TestClient):
        admin_tokens = _register(client, "admin9@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        _create_entry(client, admin_headers, concept_key="clv")
        _create_entry(client, admin_headers, concept_key="aov", business_name="Average Order Value")

        response = client.get(
            "/semantic-catalog/entries", params={"status": "draft"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_list_requires_manage_or_review_permission(self, client: TestClient):
        tokens = _register(client, "plain2@tenant-a.example.com")
        response = client.get("/semantic-catalog/entries", headers=_headers(tokens))
        assert response.status_code == 403
