"""The Prompt 32 acceptance journey, over the real HTTP API:

    client onboarding -> SME review -> semantic approval -> recommendation work

Real local accounts, real tenants, real role checks, a real in-memory identity
database, and a real (file-backed) SQLite target database standing in for the
client's warehouse. Nothing in the chain is mocked except the two boundaries
that would otherwise need live infrastructure: the target database's connection
test and its engine factory (the same seam `tests/test_api_onboarding.py` uses).

The analytics leg of the Prompt 32 chain is not in this file. Analytics
results come from `/ask`, which needs a live LLM; that path is covered by
`tests/test_full_pipeline_integration.py` with the LLM stubbed. This journey
instead proves the handoffs a person actually performs: a finished onboarding
becomes reviewable catalog knowledge, approved knowledge is governed, and a
recommendation can be worked by the same reviewer, with the tenant boundary
holding at every step.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base
from identity.repositories.recommendation_governance import create_record
from identity.repositories.tenants import create_tenant
from identity.repositories.users import create_user
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.onboarding as onboarding_mod
import api.rate_limit as api_rate_limit_mod
import api.recommendation_governance as governance_mod
from agent.provenance import DataTruthLevel, ProvenancedClaim
from config.settings import Settings
from db.connection import ConnectionTestResult

PASSWORD = "correcthorse battery staple 1"

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
    api_action_rate_limit_per_minute=1000,
)


@pytest.fixture(autouse=True)
def _identity_db(monkeypatch) -> Iterator[object]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(governance_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(
        onboarding_mod,
        "test_connection",
        lambda config: ConnectionTestResult(success=True, message="ok"),
    )

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)
    create_tenant(session, tenant_id="tenant-a", name="Tenant A")
    create_tenant(session, tenant_id="tenant-b", name="Tenant B")
    session.commit()
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()
    api_rate_limit_mod._limiters.clear()
    yield engine


@pytest.fixture
def warehouse_path(tmp_path) -> str:
    """A small real SQLite warehouse with a foreign key, so discovery produces
    tables, semantic labels and a relationship to review."""
    path = str(tmp_path / "warehouse.sqlite")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE Customers (Id INTEGER PRIMARY KEY, Email TEXT)"))
        conn.execute(
            text(
                "CREATE TABLE Orders ("
                "Id INTEGER PRIMARY KEY, CustomerId INTEGER, Amount REAL, "
                "FOREIGN KEY (CustomerId) REFERENCES Customers (Id))"
            )
        )
        for i in range(1, 4):
            conn.execute(
                text("INSERT INTO Customers VALUES (:i, :e)"), {"i": i, "e": f"c{i}@x.com"}
            )
        for i in range(1, 7):
            conn.execute(
                text("INSERT INTO Orders VALUES (:i, :c, :a)"),
                {"i": i, "c": (i % 3) + 1, "a": float(i) * 10},
            )
        conn.commit()
    engine.dispose()
    return path


@pytest.fixture
def client(monkeypatch, warehouse_path: str) -> TestClient:
    monkeypatch.setattr(
        onboarding_mod,
        "create_engine",
        lambda *args, **kwargs: create_engine(f"sqlite:///{warehouse_path}"),
    )
    return TestClient(api_main.app)


def _account(email: str, role: str, tenant_id: str = "tenant-a") -> uuid.UUID:
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        user = create_user(
            session, email=email, password=PASSWORD, tenant_id=tenant_id, role_name=role
        )
        session.commit()
        return user.id
    finally:
        session.close()


def _headers(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_recommendation(tenant_id: str = "tenant-a") -> str:
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        record = create_record(
            session,
            tenant_id=tenant_id,
            database_id="My Warehouse",
            recommendation=Recommendation(
                kind=RecommendationKind.ACTION,
                claim=ProvenancedClaim(
                    value="Review the Orders spike in March.",
                    level=DataTruthLevel.AI_INFERENCE,
                    source="recommendation.engine.AnomalyRule",
                ),
                category=RecommendationCategory.ANOMALY,
                evidence=(
                    ProvenancedClaim(
                        value="March order amount was 3x the trailing average.",
                        level=DataTruthLevel.DATABASE_FACT,
                        source="analytics.engine",
                    ),
                ),
                confidence=0.8,
                rule_or_model="recommendation.engine.AnomalyRule",
            ),
            created_by_user_id=None,
            source_question="why did orders spike?",
        )
        return str(record.id)
    finally:
        session.close()


class TestAcceptanceJourney:
    def test_onboarding_to_approved_knowledge_to_recommendation_work(self, client):
        # 1. An admin onboards a client database, the SME confirms every item,
        #    and the admin publishes.
        _account("admin@tenant-a.example.com", "admin")
        sme_id = _account("sme@tenant-a.example.com", "analyst")
        admin = _headers(client, "admin@tenant-a.example.com")
        sme = _headers(client, "sme@tenant-a.example.com")

        job = client.post(
            "/onboarding/jobs",
            json={
                "database_label": "My Warehouse",
                "db_type": "postgresql",
                "db_host": "db.internal",
                "db_port": 5432,
                "db_name": "warehouse",
                "db_user": "svc",
                "db_password": "unused-and-never-persisted",
            },
            headers=admin,
        )
        assert job.status_code == 200, job.text
        job_id = job.json()["id"]

        discover = client.post(
            f"/onboarding/jobs/{job_id}/discover", json={"db_password": "unused"}, headers=admin
        )
        assert discover.status_code == 200, discover.text
        assert discover.json()["status"] == "awaiting_review"

        items = client.get(f"/onboarding/jobs/{job_id}/review-items", headers=admin).json()
        assert items
        for item in items:
            decided = client.post(
                f"/onboarding/jobs/{job_id}/review-items/{item['id']}/decide",
                json={"decision": "confirmed"},
                headers=sme,
            )
            assert decided.status_code == 200, decided.text

        published = client.post(f"/onboarding/jobs/{job_id}/publish", json={}, headers=admin)
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published"

        # 2. Publishing created DRAFT catalog entities for the SME to approve.
        #    Nothing was published by the bridge.
        drafts = client.get(
            "/semantic-catalog/entries", params={"status": "draft"}, headers=sme
        ).json()
        assert drafts, "onboarding publish must hand the SME something to approve"
        assert all(entry["status"] == "draft" for entry in drafts)
        assert all(entry["concept_type"] == "entity" for entry in drafts)
        assert all(entry["database_id"].startswith("My_Warehouse_") for entry in drafts)
        assert all(
            entry["evidence"][0]["job_id"] == job_id for entry in drafts
        ), "every draft must trace back to the onboarding job that produced it"
        entry_id = drafts[0]["id"]

        # 3. Semantic approval: the SME approves, the admin publishes.
        reviewed = client.post(
            f"/semantic-catalog/entries/{entry_id}/review",
            json={"notes": "matches the schema"},
            headers=sme,
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "reviewed"

        published_entry = client.post(
            f"/semantic-catalog/entries/{entry_id}/publish", json={}, headers=admin
        )
        assert published_entry.status_code == 200, published_entry.text
        assert published_entry.json()["status"] == "published"

        live = client.get(
            "/semantic-catalog/entries", params={"status": "published"}, headers=sme
        ).json()
        assert entry_id in {entry["id"] for entry in live}

        # 4. Recommendation work: the same SME accepts a recommendation, takes
        #    ownership, leaves a note, and the trail records each action.
        record_id = _seed_recommendation()

        listed = client.get("/recommendations", headers=sme).json()
        assert record_id in {row["id"] for row in listed}

        verdict = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "accepted", "reason": "Confirmed against the March ledger."},
            headers=sme,
        )
        assert verdict.status_code == 200, verdict.text

        owned = client.post(
            f"/recommendations/{record_id}/owner", json={"owner_user_id": str(sme_id)}, headers=sme
        )
        assert owned.status_code == 200, owned.text
        assert owned.json()["owner_user_id"] == str(sme_id)

        noted = client.post(
            f"/recommendations/{record_id}/notes",
            json={"note": "Follow up with finance."},
            headers=sme,
        )
        assert noted.status_code == 200, noted.text

        trail = client.get(f"/recommendations/{record_id}/events", headers=sme).json()
        assert [e["event_type"] for e in trail] == [
            "status_change",
            "status_change",
            "owner_assigned",
            "note",
        ]

        # 5. Navigation for the SME reflects exactly the screens they just used.
        screens = {item["id"] for item in client.get("/navigation", headers=sme).json()["items"]}
        assert {"db_onboarding", "semantic_review", "recommendations", "chat"} <= screens
        assert "platform_admin" not in screens

    def test_the_tenant_boundary_holds_at_every_handoff(self, client):
        _account("admin@tenant-a.example.com", "admin")
        _account("other@tenant-b.example.com", "admin", tenant_id="tenant-b")
        admin_a = _headers(client, "admin@tenant-a.example.com")
        other_b = _headers(client, "other@tenant-b.example.com")

        job = client.post(
            "/onboarding/jobs",
            json={"database_label": "A-only", "db_type": "postgresql"},
            headers=admin_a,
        ).json()
        client.post(
            f"/onboarding/jobs/{job['id']}/discover",
            json={"db_password": "unused"},
            headers=admin_a,
        )
        assert client.get(f"/onboarding/jobs/{job['id']}", headers=other_b).status_code == 404

        record_id = _seed_recommendation("tenant-a")
        assert client.get(f"/recommendations/{record_id}", headers=other_b).status_code == 404
        assert record_id not in {
            r["id"] for r in client.get("/recommendations", headers=other_b).json()
        }

    def test_a_job_missing_its_connection_details_is_a_422_and_stays_pending(self, client):
        """Regression: this used to surface as an unhandled 500. It is a
        client-correctable state, so it answers 422 and the job is left pending
        for the caller to see."""
        _account("admin@tenant-a.example.com", "admin")
        admin = _headers(client, "admin@tenant-a.example.com")
        job = client.post(
            "/onboarding/jobs",
            json={"database_label": "Incomplete", "db_type": "postgresql"},
            headers=admin,
        ).json()

        response = client.post(
            f"/onboarding/jobs/{job['id']}/discover", json={"db_password": "unused"}, headers=admin
        )

        assert response.status_code == 422
        assert "missing connection details" in response.json()["detail"]
        assert "DB_HOST" not in response.text
        assert (
            client.get(f"/onboarding/jobs/{job['id']}", headers=admin).json()["status"] == "pending"
        )

    def test_a_business_user_cannot_approve_or_publish_knowledge(self, client):
        _account("user@tenant-a.example.com", "user")
        user = _headers(client, "user@tenant-a.example.com")
        assert client.get("/semantic-catalog/entries", headers=user).status_code == 403
        assert client.get("/recommendations", headers=user).status_code == 403
        assert client.get("/onboarding/jobs", headers=user).status_code == 403
