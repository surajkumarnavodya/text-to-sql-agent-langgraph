"""End-to-end HTTP tests for api/onboarding.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- same `TestClient` +
in-memory-SQLite-identity-DB pattern as `tests/test_api_shares.py`.

Two users are never enough to prove tenant isolation on their own (this
app has no real multi-tenant column outside a handful of new-feature
tables -- see `security/tenancy.py`'s own docstring), so
`_fake_tenant_resolver` below simulates two tenants deterministically by
email domain, monkeypatched onto `api.onboarding.resolve_actor_tenant_id`
-- this exercises the *real* ABAC tenant check in
`onboarding.policy.authorize_onboarding_action`, not a mocked stand-in
for it.

The "target" database being onboarded is a real, file-backed SQLite
database (not `:memory:` -- a route disposes its own throwaway engine in
a `finally` block every request, which would lose an in-memory database's
data on the very next connection) seeded with two FK-related tables.
`api.onboarding.create_engine`/`test_connection` are monkeypatched so a
job's cosmetic `db_type="postgresql"` never attempts a real network dial
-- this file tests the onboarding REST surface's own authorization/
state-machine wiring, not a real Postgres driver.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, User
from identity.repositories.users import assign_role
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.onboarding as onboarding_mod
from config.settings import Settings
from db.connection import ConnectionTestResult

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
    monkeypatch.setattr(onboarding_mod, "resolve_actor_tenant_id", _fake_tenant_resolver)
    monkeypatch.setattr(
        onboarding_mod,
        "test_connection",
        lambda config: ConnectionTestResult(success=True, message="ok"),
    )

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()

    yield engine


@pytest.fixture
def target_db_path(tmp_path) -> Iterator[str]:
    path = str(tmp_path / "target.sqlite")
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
    yield path


@pytest.fixture
def client(monkeypatch, target_db_path: str) -> TestClient:
    def _fake_create_engine(*args, **kwargs):
        return create_engine(f"sqlite:///{target_db_path}")

    monkeypatch.setattr(onboarding_mod, "create_engine", _fake_create_engine)
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


def _create_job(client: TestClient, headers: dict) -> dict:
    response = client.post(
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
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestCreateJobRbac:
    def test_admin_can_create_a_job(self, client: TestClient):
        tokens = _register(client, "admin@tenant-a.example.com", role="admin")
        job = _create_job(client, _headers(tokens))
        assert job["status"] == "pending"
        assert "db_password" not in job

    def test_plain_user_without_onboarding_manage_is_forbidden(self, client: TestClient):
        tokens = _register(client, "plain@tenant-a.example.com")
        response = client.post(
            "/onboarding/jobs",
            json={"database_label": "x", "db_type": "postgresql"},
            headers=_headers(tokens),
        )
        assert response.status_code == 403

    def test_analyst_alone_cannot_create_a_job(self, client: TestClient):
        tokens = _register(client, "analyst@tenant-a.example.com", role="analyst")
        response = client.post(
            "/onboarding/jobs",
            json={"database_label": "x", "db_type": "postgresql"},
            headers=_headers(tokens),
        )
        assert response.status_code == 403


class TestTenantIsolation:
    def test_a_job_created_in_one_tenant_is_invisible_to_an_admin_in_another(
        self, client: TestClient
    ):
        owner_tokens = _register(client, "owner@tenant-a.example.com", role="admin")
        job = _create_job(client, _headers(owner_tokens))

        other_tenant_tokens = _register(client, "other@tenant-b.example.com", role="admin")
        response = client.get(
            f"/onboarding/jobs/{job['id']}", headers=_headers(other_tenant_tokens)
        )
        assert response.status_code == 404

    def test_a_random_job_id_is_also_a_404_not_a_403(self, client: TestClient):
        tokens = _register(client, "admin2@tenant-a.example.com", role="admin")
        response = client.get(f"/onboarding/jobs/{uuid.uuid4()}", headers=_headers(tokens))
        assert response.status_code == 404


class TestFullLifecycle:
    def test_create_discover_review_publish(self, client: TestClient):
        admin_tokens = _register(client, "admin3@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        job = _create_job(client, admin_headers)
        job_id = job["id"]

        discover_response = client.post(
            f"/onboarding/jobs/{job_id}/discover",
            json={"db_password": "unused"},
            headers=admin_headers,
        )
        assert discover_response.status_code == 200, discover_response.text
        discovered = discover_response.json()
        assert discovered["status"] == "awaiting_review"
        assert discovered["discovery_summary"]["table_count"] == 2

        items_response = client.get(
            f"/onboarding/jobs/{job_id}/review-items", headers=admin_headers
        )
        assert items_response.status_code == 200
        items = items_response.json()
        assert len(items) > 0

        analyst_tokens = _register(client, "analyst2@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)
        for item in items:
            decide_response = client.post(
                f"/onboarding/jobs/{job_id}/review-items/{item['id']}/decide",
                json={"decision": "confirmed"},
                headers=analyst_headers,
            )
            assert decide_response.status_code == 200, decide_response.text

        # An analyst (ONBOARDING_REVIEW only) cannot publish.
        forbidden_publish = client.post(
            f"/onboarding/jobs/{job_id}/publish", json={}, headers=analyst_headers
        )
        assert forbidden_publish.status_code == 403

        publish_response = client.post(
            f"/onboarding/jobs/{job_id}/publish", json={}, headers=admin_headers
        )
        assert publish_response.status_code == 200, publish_response.text
        assert publish_response.json()["status"] == "published"

        artifacts_response = client.get(
            f"/onboarding/jobs/{job_id}/artifacts", headers=admin_headers
        )
        assert artifacts_response.status_code == 200
        artifact_types = {a["artifact_type"] for a in artifacts_response.json()}
        assert artifact_types == {"semantic_contract", "golden_questions", "evaluation_report"}

    def test_publishing_with_a_pending_item_is_rejected(self, client: TestClient):
        admin_tokens = _register(client, "admin4@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        job = _create_job(client, admin_headers)
        job_id = job["id"]
        client.post(f"/onboarding/jobs/{job_id}/discover", json={}, headers=admin_headers)

        response = client.post(f"/onboarding/jobs/{job_id}/publish", json={}, headers=admin_headers)
        assert response.status_code == 409


class TestCancelAndRetry:
    def test_admin_can_cancel_a_pending_job(self, client: TestClient):
        admin_tokens = _register(client, "admin5@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        job = _create_job(client, admin_headers)

        response = client.post(f"/onboarding/jobs/{job['id']}/cancel", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

    def test_cancelling_an_already_cancelled_job_is_a_conflict(self, client: TestClient):
        admin_tokens = _register(client, "admin6@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        job = _create_job(client, admin_headers)
        client.post(f"/onboarding/jobs/{job['id']}/cancel", headers=admin_headers)

        response = client.post(f"/onboarding/jobs/{job['id']}/cancel", headers=admin_headers)
        assert response.status_code == 409

    def test_retry_resets_a_failed_discovery_back_to_pending(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        admin_tokens = _register(client, "admin7@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        job = _create_job(client, admin_headers)
        job_id = job["id"]

        import onboarding.jobs as jobs_mod

        original_introspect = jobs_mod.introspect_schema

        def _boom(*args, **kwargs):
            raise RuntimeError("introspection exploded")

        monkeypatch.setattr(jobs_mod, "introspect_schema", _boom)

        discover_response = client.post(
            f"/onboarding/jobs/{job_id}/discover", json={}, headers=admin_headers
        )
        assert discover_response.status_code == 200
        assert discover_response.json()["status"] == "failed"

        # Restore the real function explicitly -- `onboarding/jobs.py`'s
        # own retry path doesn't need it (retry only resets job state),
        # but leaving the patch in place would be a latent trap for any
        # later assertion in this test.
        monkeypatch.setattr(jobs_mod, "introspect_schema", original_introspect)

        retry_response = client.post(f"/onboarding/jobs/{job_id}/retry", headers=admin_headers)
        assert retry_response.status_code == 200
        assert retry_response.json()["status"] == "pending"


class TestConnectionTestRateLimit:
    """Prompt 21 (enterprise security & data governance hardening):
    `POST /onboarding/jobs`, `.../discover`, and `.../publish` each open a
    live outbound connection to a caller-supplied db_host/db_port --
    admin-gated already, but previously with no rate limit at all, which
    made `POST /onboarding/jobs` a repeatable TCP-connect oracle against
    any host/port the caller names."""

    @pytest.fixture(autouse=True)
    def _reset_onboarding_connection_limiters(self):
        import agent.rate_limit as rate_limit_mod

        rate_limit_mod._onboarding_connection_test_limiters.clear()
        yield
        rate_limit_mod._onboarding_connection_test_limiters.clear()

    def test_create_job_trips_after_the_configured_hourly_limit(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        low_limit_settings = Settings(
            **{**_SETTINGS.__dict__, "onboarding_connection_rate_limit_per_hour": 1}
        )
        monkeypatch.setattr(onboarding_mod, "get_settings", lambda: low_limit_settings)
        tokens = _register(client, "admin8@tenant-a.example.com", role="admin")

        first = client.post(
            "/onboarding/jobs",
            json={
                "database_label": "First",
                "db_type": "postgresql",
                "db_host": "db.internal",
                "db_port": 5432,
                "db_name": "warehouse",
                "db_user": "svc",
                "db_password": "unused",
            },
            headers=_headers(tokens),
        )
        second = client.post(
            "/onboarding/jobs",
            json={
                "database_label": "Second",
                "db_type": "postgresql",
                "db_host": "another.internal",
                "db_port": 5432,
                "db_name": "warehouse2",
                "db_user": "svc",
                "db_password": "unused",
            },
            headers=_headers(tokens),
        )

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_discover_and_publish_are_also_rate_limited(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        # The limiter is created on first use with whatever max_events its
        # *first* caller passed, and ignores a different value on later
        # calls for the same key (`agent.rate_limit.BoundedLimiterCache
        # .get_or_create`'s own documented first-call-wins semantics) --
        # so the low limit must already be active for job creation too,
        # which is what consumes the single slot this test then proves
        # `discover` also respects.
        low_limit_settings = Settings(
            **{**_SETTINGS.__dict__, "onboarding_connection_rate_limit_per_hour": 1}
        )
        monkeypatch.setattr(onboarding_mod, "get_settings", lambda: low_limit_settings)
        tokens = _register(client, "admin9@tenant-a.example.com", role="admin")
        job = _create_job(client, _headers(tokens))

        discover_response = client.post(
            f"/onboarding/jobs/{job['id']}/discover", json={}, headers=_headers(tokens)
        )
        assert discover_response.status_code == 429
        assert "Retry-After" in discover_response.headers

    def test_different_admins_get_independent_budgets(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """The limiter is keyed per-user -- one admin exhausting their own
        budget must never block a different admin."""
        low_limit_settings = Settings(
            **{**_SETTINGS.__dict__, "onboarding_connection_rate_limit_per_hour": 1}
        )
        monkeypatch.setattr(onboarding_mod, "get_settings", lambda: low_limit_settings)
        tokens_a = _register(client, "admin10@tenant-a.example.com", role="admin")
        tokens_b = _register(client, "admin11@tenant-b.example.com", role="admin")

        job_payload = {
            "database_label": "x",
            "db_type": "postgresql",
            "db_host": "db.internal",
            "db_port": 5432,
            "db_name": "warehouse",
            "db_user": "svc",
            "db_password": "unused",
        }

        first_admin_first_call = client.post(
            "/onboarding/jobs", json=job_payload, headers=_headers(tokens_a)
        )
        second_admin_first_call = client.post(
            "/onboarding/jobs", json=job_payload, headers=_headers(tokens_b)
        )

        assert first_admin_first_call.status_code == 200
        assert second_admin_first_call.status_code == 200


class TestReviewDecisionAudit:
    """Prompt 27 (`27_SME_SEMANTIC_REVIEW_DASHBOARD_CONTRACT.md`)'s own
    "audit" testing requirement -- an SME's confirm/reject decision on an
    onboarding review item is now a structured `security.audit_log`
    event, not silently unaudited."""

    def test_deciding_a_review_item_emits_a_security_audit_event(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple] = []
        monkeypatch.setattr(
            onboarding_mod,
            "log_security_event",
            lambda *args, **kwargs: events.append((args, kwargs)),
        )

        admin_tokens = _register(client, "admin12@tenant-a.example.com", role="admin")
        admin_headers = _headers(admin_tokens)
        job = _create_job(client, admin_headers)
        client.post(
            f"/onboarding/jobs/{job['id']}/discover",
            json={"db_password": "unused"},
            headers=admin_headers,
        )
        items = client.get(
            f"/onboarding/jobs/{job['id']}/review-items", headers=admin_headers
        ).json()
        assert items

        analyst_tokens = _register(client, "analyst3@tenant-a.example.com", role="analyst")
        analyst_headers = _headers(analyst_tokens)
        client.post(
            f"/onboarding/jobs/{job['id']}/review-items/{items[0]['id']}/decide",
            json={"decision": "confirmed"},
            headers=analyst_headers,
        )

        assert len(events) == 1
        (event_type, severity, _detail), kwargs = events[0]
        assert event_type == "onboarding_review_item_decided"
        assert severity == "info"
        assert kwargs["decision"] == "confirmed"
        assert kwargs["job_id"] == job["id"]
        assert kwargs["item_id"] == items[0]["id"]


class TestReviewDecisionsAreFrozenOnceJobLeavesReview:
    """A review decision is the input the published semantic contract was
    built from (`onboarding/semantic_contract.py` only includes `"confirmed"`
    items). Changing a decision after publish would silently diverge the
    recorded decisions from the artifact actually published -- so the route
    must refuse it once the job is no longer `awaiting_review`."""

    def test_deciding_an_item_on_a_published_job_is_a_conflict(self, client: TestClient):
        admin_headers = _headers(_register(client, "admin30@tenant-a.example.com", role="admin"))
        job_id = _create_job(client, admin_headers)["id"]
        client.post(
            f"/onboarding/jobs/{job_id}/discover",
            json={"db_password": "unused"},
            headers=admin_headers,
        )
        items = client.get(f"/onboarding/jobs/{job_id}/review-items", headers=admin_headers).json()
        assert items

        analyst_headers = _headers(
            _register(client, "analyst30@tenant-a.example.com", role="analyst")
        )
        for item in items:
            client.post(
                f"/onboarding/jobs/{job_id}/review-items/{item['id']}/decide",
                json={"decision": "confirmed"},
                headers=analyst_headers,
            )
        publish = client.post(f"/onboarding/jobs/{job_id}/publish", json={}, headers=admin_headers)
        assert publish.status_code == 200, publish.text

        target = items[0]
        response = client.post(
            f"/onboarding/jobs/{job_id}/review-items/{target['id']}/decide",
            json={"decision": "rejected"},
            headers=analyst_headers,
        )
        assert response.status_code == 409

        after = client.get(f"/onboarding/jobs/{job_id}/review-items", headers=admin_headers).json()
        recorded = next(item for item in after if item["id"] == target["id"])
        assert recorded["decision"] == "confirmed"
