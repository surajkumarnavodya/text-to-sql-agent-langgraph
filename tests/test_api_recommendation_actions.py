"""End-to-end HTTP tests for Prompt 31's recommendation actions -- notes,
owner assignment, ownership list filters, structured logging, and the
per-action rate limit -- in `api/recommendation_governance.py`.

Unlike `tests/test_api_recommendation_governance.py`, which fakes tenancy
by email domain, these tests use the **real** tenant resolver
(`security.tenancy.resolve_actor_tenant_id`) against real `tenants` rows and
real `users.tenant_id` values, because the owner-assignment check compares
the stored tenant column itself. Users are created directly with a chosen
tenant and role, then authenticated through the real `/auth/login` route.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, User
from identity.repositories.recommendation_governance import create_record
from identity.repositories.tenants import create_tenant
from identity.repositories.users import assign_role, create_user
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.rate_limit as api_rate_limit_mod
import api.recommendation_governance as governance_mod
from agent.provenance import DataTruthLevel, ProvenancedClaim
from config.settings import Settings

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
def _identity_test_db(monkeypatch) -> Iterator[object]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(governance_mod, "get_settings", lambda: _SETTINGS)

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
def client() -> TestClient:
    return TestClient(api_main.app)


def _make_user(
    email: str, *, tenant_id: str, role: str | None = None, display_name: str | None = None
) -> uuid.UUID:
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        user = create_user(
            session,
            email=email,
            password=PASSWORD,
            display_name=display_name,
            tenant_id=tenant_id,
        )
        if role is not None:
            assign_role(session, user_id=user.id, role_name=role, assigned_by_user_id=None)
        session.commit()
        return user.id
    finally:
        session.close()


def _login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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


def _seed_record(tenant_id: str = "tenant-a", **overrides) -> str:
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        record = create_record(
            session,
            tenant_id=tenant_id,
            database_id="hr",
            recommendation=_recommendation(**overrides),
            created_by_user_id=None,
            source_question="why did sales spike?",
            source_sql="SELECT 1",
        )
        return str(record.id)
    finally:
        session.close()


class TestNotes:
    def test_analyst_adds_a_note_without_changing_status(self, client: TestClient):
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("n1@a.example", tenant_id="tenant-a", role="analyst"))
        )
        response = client.post(
            f"/recommendations/{record_id}/notes",
            json={"note": "  Checked with finance, looks right.  "},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["event_type"] == "note"
        assert body["reason"] == "Checked with finance, looks right."
        assert body["from_status"] == body["to_status"] == "generated"

        record = client.get(f"/recommendations/{record_id}", headers=headers).json()
        assert record["status"] == "generated"

    def test_note_appears_in_the_audit_trail_in_order(self, client: TestClient):
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("n2@a.example", tenant_id="tenant-a", role="analyst"))
        )
        client.post(f"/recommendations/{record_id}/notes", json={"note": "first"}, headers=headers)
        client.post(
            f"/recommendations/{record_id}/feedback", json={"status": "accepted"}, headers=headers
        )
        client.post(f"/recommendations/{record_id}/notes", json={"note": "second"}, headers=headers)

        events = client.get(f"/recommendations/{record_id}/events", headers=headers).json()
        assert [e["event_type"] for e in events] == [
            "status_change",
            "note",
            "status_change",
            "note",
        ]
        assert events[1]["reason"] == "first"
        assert events[3]["reason"] == "second"

    def test_empty_note_is_rejected(self, client: TestClient):
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("n3@a.example", tenant_id="tenant-a", role="analyst"))
        )
        response = client.post(
            f"/recommendations/{record_id}/notes", json={"note": "   "}, headers=headers
        )
        assert response.status_code == 422

    def test_plain_user_cannot_add_a_note(self, client: TestClient):
        record_id = _seed_record()
        headers = _login(client, _email(_make_user("n4@a.example", tenant_id="tenant-a")))
        response = client.post(
            f"/recommendations/{record_id}/notes", json={"note": "hi"}, headers=headers
        )
        assert response.status_code == 403

    def test_cross_tenant_note_is_a_404(self, client: TestClient):
        record_id = _seed_record(tenant_id="tenant-a")
        headers = _login(
            client, _email(_make_user("n5@b.example", tenant_id="tenant-b", role="admin"))
        )
        response = client.post(
            f"/recommendations/{record_id}/notes", json={"note": "hi"}, headers=headers
        )
        assert response.status_code == 404


class TestOwnerAssignment:
    def test_analyst_assigns_themselves_and_gets_a_display_name(self, client: TestClient):
        analyst_id = _make_user(
            "o1@a.example", tenant_id="tenant-a", role="analyst", display_name="Ola Analyst"
        )
        record_id = _seed_record()
        headers = _login(client, "o1@a.example")
        response = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_id)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["owner_user_id"] == str(analyst_id)
        assert body["owner_display_name"] == "Ola Analyst"

    def test_owner_without_a_display_name_is_null_not_an_email(self, client: TestClient):
        analyst_id = _make_user("o2@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "o2@a.example")
        body = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_id)},
            headers=headers,
        ).json()
        assert body["owner_display_name"] is None
        assert "@" not in (body["owner_display_name"] or "")

    def test_clearing_the_owner_with_null(self, client: TestClient):
        analyst_id = _make_user("o3@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "o3@a.example")
        client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_id)},
            headers=headers,
        )
        body = client.post(
            f"/recommendations/{record_id}/owner", json={"owner_user_id": None}, headers=headers
        ).json()
        assert body["owner_user_id"] is None

        events = client.get(f"/recommendations/{record_id}/events", headers=headers).json()
        owner_events = [e for e in events if e["event_type"] == "owner_assigned"]
        assert len(owner_events) == 2
        assert owner_events[-1]["detail"] == {
            "owner_user_id": None,
            "previous_owner_user_id": str(analyst_id),
        }

    def test_owner_event_records_new_and_previous_owner(self, client: TestClient):
        analyst_id = _make_user("o4@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "o4@a.example")
        client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_id)},
            headers=headers,
        )
        events = client.get(f"/recommendations/{record_id}/events", headers=headers).json()
        owner_event = next(e for e in events if e["event_type"] == "owner_assigned")
        assert owner_event["from_status"] == owner_event["to_status"] == "generated"
        assert owner_event["detail"] == {
            "owner_user_id": str(analyst_id),
            "previous_owner_user_id": None,
        }

    def test_assigning_a_user_who_cannot_review_is_a_404(self, client: TestClient):
        plain_id = _make_user("o5p@a.example", tenant_id="tenant-a")
        _make_user("o5@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "o5@a.example")
        response = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(plain_id)},
            headers=headers,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Owner not found in this tenant."

    def test_cross_tenant_owner_is_indistinguishable_from_a_nonexistent_one(
        self, client: TestClient
    ):
        """The anti-enumeration guarantee: a real reviewer in another tenant
        and a random id must produce the same status and the same body."""
        other_tenant_id = _make_user("o6b@b.example", tenant_id="tenant-b", role="analyst")
        _make_user("o6@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "o6@a.example")

        cross = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(other_tenant_id)},
            headers=headers,
        )
        nonexistent = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert cross.status_code == nonexistent.status_code == 404
        assert cross.json() == nonexistent.json()

    def test_plain_user_cannot_assign_an_owner(self, client: TestClient):
        plain_id = _make_user("o7@a.example", tenant_id="tenant-a")
        record_id = _seed_record()
        headers = _login(client, "o7@a.example")
        response = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(plain_id)},
            headers=headers,
        )
        assert response.status_code == 403

    def test_cross_tenant_caller_cannot_assign_on_a_record(self, client: TestClient):
        analyst_b = _make_user("o8@b.example", tenant_id="tenant-b", role="analyst")
        record_id = _seed_record(tenant_id="tenant-a")
        headers = _login(client, "o8@b.example")
        response = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_b)},
            headers=headers,
        )
        assert response.status_code == 404

    def test_unknown_field_in_the_body_is_rejected(self, client: TestClient):
        analyst_id = _make_user("o9@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "o9@a.example")
        response = client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_id), "tenant_id": "tenant-b"},
            headers=headers,
        )
        assert response.status_code == 422


class TestOwnershipFilters:
    def test_filter_by_owner_and_unassigned(self, client: TestClient):
        analyst_id = _make_user("f1@a.example", tenant_id="tenant-a", role="analyst")
        headers = _login(client, "f1@a.example")
        owned = _seed_record()
        _seed_record()  # stays unassigned
        client.post(
            f"/recommendations/{owned}/owner",
            json={"owner_user_id": str(analyst_id)},
            headers=headers,
        )

        mine = client.get(
            "/recommendations", params={"owner_user_id": str(analyst_id)}, headers=headers
        ).json()
        assert [r["id"] for r in mine] == [owned]

        unassigned = client.get(
            "/recommendations", params={"unassigned": "true"}, headers=headers
        ).json()
        assert len(unassigned) == 1
        assert unassigned[0]["id"] != owned

    def test_unassigned_takes_precedence_over_an_owner_filter(self, client: TestClient):
        analyst_id = _make_user("f2@a.example", tenant_id="tenant-a", role="analyst")
        headers = _login(client, "f2@a.example")
        record_id = _seed_record()
        client.post(
            f"/recommendations/{record_id}/owner",
            json={"owner_user_id": str(analyst_id)},
            headers=headers,
        )
        result = client.get(
            "/recommendations",
            params={"unassigned": "true", "owner_user_id": str(analyst_id)},
            headers=headers,
        ).json()
        assert result == []

    def test_a_malformed_owner_filter_is_a_422_not_a_500(self, client: TestClient):
        _make_user("f4@a.example", tenant_id="tenant-a", role="analyst")
        headers = _login(client, "f4@a.example")
        response = client.get(
            "/recommendations", params={"owner_user_id": "not-a-uuid"}, headers=headers
        )
        assert response.status_code == 422

    def test_list_carries_owner_display_names_for_a_batch(self, client: TestClient):
        analyst_id = _make_user(
            "f3@a.example", tenant_id="tenant-a", role="analyst", display_name="Batch Owner"
        )
        headers = _login(client, "f3@a.example")
        for _ in range(3):
            record_id = _seed_record()
            client.post(
                f"/recommendations/{record_id}/owner",
                json={"owner_user_id": str(analyst_id)},
                headers=headers,
            )
        rows = client.get("/recommendations", headers=headers).json()
        assert len(rows) == 3
        assert {row["owner_display_name"] for row in rows} == {"Batch Owner"}


class TestLifecycleStillWorks:
    def test_accept_resolve_then_note_on_a_resolved_record(self, client: TestClient):
        """Notes are allowed on any status -- they are commentary, not a
        transition -- including a terminal one."""
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("l1@a.example", tenant_id="tenant-a", role="analyst"))
        )
        client.post(
            f"/recommendations/{record_id}/feedback", json={"status": "accepted"}, headers=headers
        )
        client.post(f"/recommendations/{record_id}/resolve", json={}, headers=headers)
        response = client.post(
            f"/recommendations/{record_id}/notes",
            json={"note": "post-mortem filed"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["from_status"] == "resolved"

    def test_partially_useful_verdict_is_accepted_by_the_api(self, client: TestClient):
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("l2@a.example", tenant_id="tenant-a", role="analyst"))
        )
        response = client.post(
            f"/recommendations/{record_id}/feedback",
            json={"status": "partially_useful", "reason": "Right direction, wrong period."},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "partially_useful"


class TestStructuredLogging:
    def test_every_mutation_emits_one_structured_action_line(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("g1@a.example", tenant_id="tenant-a", role="analyst"))
        )
        with caplog.at_level(logging.INFO):
            client.post(
                f"/recommendations/{record_id}/feedback",
                json={"status": "accepted"},
                headers=headers,
            )
            client.post(f"/recommendations/{record_id}/notes", json={"note": "x"}, headers=headers)
        lines = [
            r.getMessage()
            for r in caplog.records
            if "event=recommendation_action" in r.getMessage()
        ]
        assert len(lines) == 2
        assert "action='feedback'" in lines[0]
        assert "from_status='generated'" in lines[0]
        assert "to_status='accepted'" in lines[0]
        assert "action='note'" in lines[1]

    def test_log_lines_never_contain_claim_or_note_text(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("g2@a.example", tenant_id="tenant-a", role="analyst"))
        )
        with caplog.at_level(logging.INFO):
            client.post(
                f"/recommendations/{record_id}/notes",
                json={"note": "SECRET-NOTE-BODY-42"},
                headers=headers,
            )
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRET-NOTE-BODY-42" not in combined
        assert "Investigate the anomalous spike" not in combined

    def test_refused_owner_assignment_is_logged_as_a_warning(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ):
        other = _make_user("g3b@b.example", tenant_id="tenant-b", role="analyst")
        _make_user("g3@a.example", tenant_id="tenant-a", role="analyst")
        record_id = _seed_record()
        headers = _login(client, "g3@a.example")
        with caplog.at_level(logging.WARNING):
            client.post(
                f"/recommendations/{record_id}/owner",
                json={"owner_user_id": str(other)},
                headers=headers,
            )
        assert any("event=recommendation_owner_rejected" in r.getMessage() for r in caplog.records)


class TestRateLimit:
    def test_a_second_note_within_the_window_is_429(self, client: TestClient, monkeypatch):
        limited = Settings(**{**_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr(governance_mod, "get_settings", lambda: limited)
        record_id = _seed_record()
        headers = _login(
            client, _email(_make_user("rl1@a.example", tenant_id="tenant-a", role="analyst"))
        )
        first = client.post(
            f"/recommendations/{record_id}/notes", json={"note": "one"}, headers=headers
        )
        second = client.post(
            f"/recommendations/{record_id}/notes", json={"note": "two"}, headers=headers
        )
        assert first.status_code == 200
        assert second.status_code == 429

    def test_an_unauthorized_call_does_not_spend_the_budget(self, client: TestClient, monkeypatch):
        """Authorization runs before the rate limiter, so a denied caller
        cannot exhaust a legitimate reviewer's budget."""
        limited = Settings(**{**_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr(governance_mod, "get_settings", lambda: limited)
        record_id = _seed_record()
        plain = _login(client, _email(_make_user("rl2p@a.example", tenant_id="tenant-a")))
        for _ in range(3):
            client.post(f"/recommendations/{record_id}/notes", json={"note": "x"}, headers=plain)
        reviewer = _login(
            client, _email(_make_user("rl2@a.example", tenant_id="tenant-a", role="analyst"))
        )
        response = client.post(
            f"/recommendations/{record_id}/notes", json={"note": "ok"}, headers=reviewer
        )
        assert response.status_code == 200


def _email(user_id: uuid.UUID) -> str:
    """Looks up the email for a user id created by `_make_user`, so a test
    can log in without threading both values through every call."""
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        return session.get(User, user_id).email
    finally:
        session.close()
