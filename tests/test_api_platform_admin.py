"""End-to-end HTTP tests for api/platform_admin.py (Prompt 28,
`28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`) -- same `TestClient` +
in-memory-SQLite-identity-DB pattern as `tests/test_api_onboarding.py`/
`tests/test_api_semantic_catalog.py`.

**This module's own entire reason to exist is cross-tenant visibility**,
so unlike those two sibling test files, there is no `_fake_tenant_resolver`
monkeypatch here at all -- `api/platform_admin.py` never calls
`resolve_actor_tenant_id`, by design (see that module's own docstring).
Instead, two *real* tenants and real accounts bound to each are created
directly via `identity.repositories.tenants.create_tenant`/
`identity.repositories.users.create_user`'s own `tenant_id` parameter --
exactly the "an operator running code directly against the database"
path those functions' own docstrings describe, since there is still no
self-service tenant selection anywhere in this app to abuse.
"""

from __future__ import annotations

import uuid

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base
from identity.repositories.tenants import create_tenant
from identity.repositories.users import assign_role, create_user, get_user_by_email
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
from config.settings import Settings
from security.audit_log import log_security_event

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

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)
    create_tenant(session, tenant_id="tenant-a", name="Tenant A")
    create_tenant(session, tenant_id="tenant-b", name="Tenant B")
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()

    yield engine


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _register_in_tenant(client: TestClient, email: str, role: str, tenant_id: str) -> dict:
    """Creates a real account bound to `tenant_id` directly via the
    repository layer (never through `/auth/register`, which always
    lands a new account in the default tenant -- there is no
    self-service tenant selection anywhere in this app), then logs it
    in through the real `/auth/login` route so this test exercises a
    genuine, server-issued token."""
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        if get_user_by_email(session, email) is None:
            create_user(
                session,
                email=email,
                password="correcthorse battery staple 1",
                display_name=email.split("@")[0],
                tenant_id=tenant_id,
            )
        user = get_user_by_email(session, email)
        assert user is not None
        assign_role(session, user_id=user.id, role_name=role, assigned_by_user_id=None)
    finally:
        session.close()

    response = client.post(
        "/auth/login",
        json={"email": email, "password": "correcthorse battery staple 1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestPlatformAdminVsTenantAdminAccess:
    """The prompt's own explicit testing requirement: a tenant-scoped
    `admin` must never reach this dashboard just by holding that role --
    only the separate `platform_admin` role (`Permission.PLATFORM_ADMIN`)
    does."""

    def test_a_tenant_admin_is_forbidden(self, client: TestClient):
        tokens = _register_in_tenant(client, "admin@tenant-a.example.com", "admin", "tenant-a")
        response = client.get("/platform-admin/tenants", headers=_headers(tokens))
        assert response.status_code == 403

    def test_an_analyst_is_forbidden(self, client: TestClient):
        tokens = _register_in_tenant(client, "analyst@tenant-a.example.com", "analyst", "tenant-a")
        response = client.get("/platform-admin/tenants", headers=_headers(tokens))
        assert response.status_code == 403

    def test_a_plain_user_is_forbidden(self, client: TestClient):
        tokens = _register_in_tenant(client, "user@tenant-a.example.com", "user", "tenant-a")
        response = client.get("/platform-admin/tenants", headers=_headers(tokens))
        assert response.status_code == 403

    def test_a_platform_admin_is_allowed(self, client: TestClient):
        tokens = _register_in_tenant(
            client, "platform@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.get("/platform-admin/tenants", headers=_headers(tokens))
        assert response.status_code == 200

    def test_an_unauthenticated_caller_gets_401_or_403_never_data(self, client: TestClient):
        response = client.get("/platform-admin/tenants")
        assert response.status_code in (401, 403)


class TestCrossTenantDataVisibility:
    """ "Test... dashboard data authorization": a platform admin's own
    queries must genuinely span every tenant, not just their own."""

    def test_tenants_list_includes_both_tenants_with_correct_user_counts(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform2@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        _register_in_tenant(client, "other-user@tenant-b.example.com", "user", "tenant-b")

        response = client.get("/platform-admin/tenants", headers=_headers(platform_tokens))
        assert response.status_code == 200
        by_id = {tenant["id"]: tenant for tenant in response.json()}
        assert "tenant-a" in by_id
        assert "tenant-b" in by_id
        # tenant-a: the platform admin account itself.
        assert by_id["tenant-a"]["user_count"] >= 1
        # tenant-b: the just-created plain user.
        assert by_id["tenant-b"]["user_count"] >= 1

    def test_users_list_spans_both_tenants(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform3@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        _register_in_tenant(client, "visible-b@tenant-b.example.com", "user", "tenant-b")

        response = client.get("/platform-admin/users", headers=_headers(platform_tokens))
        assert response.status_code == 200
        tenant_ids_seen = {user["tenant_id"] for user in response.json()}
        assert "tenant-a" in tenant_ids_seen
        assert "tenant-b" in tenant_ids_seen

    def test_users_list_can_be_filtered_to_one_tenant(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform4@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        _register_in_tenant(client, "visible-b2@tenant-b.example.com", "user", "tenant-b")

        response = client.get(
            "/platform-admin/users",
            params={"tenant_id": "tenant-b"},
            headers=_headers(platform_tokens),
        )
        assert response.status_code == 200
        assert all(user["tenant_id"] == "tenant-b" for user in response.json())
        assert len(response.json()) >= 1


class TestTenantLifecycle:
    def test_create_suspend_and_reactivate_a_tenant_is_audited(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform5@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        headers = _headers(platform_tokens)

        create_response = client.post(
            "/platform-admin/tenants",
            json={"tenant_id": "tenant-c", "name": "Tenant C"},
            headers=headers,
        )
        assert create_response.status_code == 200, create_response.text
        assert create_response.json()["status"] == "active"

        suspend_response = client.post(
            "/platform-admin/tenants/tenant-c/status",
            json={"status": "suspended"},
            headers=headers,
        )
        assert suspend_response.status_code == 200
        assert suspend_response.json()["status"] == "suspended"

        reactivate_response = client.post(
            "/platform-admin/tenants/tenant-c/status",
            json={"status": "active"},
            headers=headers,
        )
        assert reactivate_response.status_code == 200
        assert reactivate_response.json()["status"] == "active"

        audit_response = client.get(
            "/platform-admin/audit-logs",
            params={"resource_type": "tenant", "resource_id": "tenant-c"},
            headers=headers,
        )
        assert audit_response.status_code == 200
        actions = [event["action"] for event in audit_response.json()]
        assert "tenant_created" in actions
        assert actions.count("tenant_status_changed") == 2

    def test_a_tenant_admin_cannot_suspend_a_tenant(self, client: TestClient):
        tenant_admin_tokens = _register_in_tenant(
            client, "admin6@tenant-a.example.com", "admin", "tenant-a"
        )
        response = client.post(
            "/platform-admin/tenants/tenant-b/status",
            json={"status": "suspended"},
            headers=_headers(tenant_admin_tokens),
        )
        assert response.status_code == 403

    def test_suspending_an_unknown_tenant_is_404(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform6@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.post(
            "/platform-admin/tenants/does-not-exist/status",
            json={"status": "suspended"},
            headers=_headers(platform_tokens),
        )
        assert response.status_code == 404


class TestRoleAssignmentAndRemoval:
    def test_assigning_and_removing_a_role_is_audited(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform7@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        headers = _headers(platform_tokens)
        target_tokens = _register_in_tenant(
            client, "target@tenant-b.example.com", "user", "tenant-b"
        )
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            target_user = get_user_by_email(session, "target@tenant-b.example.com")
            assert target_user is not None
            target_id = str(target_user.id)
        finally:
            session.close()
        del target_tokens  # only needed to prove the account exists via login above

        assign_response = client.post(
            f"/platform-admin/users/{target_id}/roles",
            json={"role_name": "analyst"},
            headers=headers,
        )
        assert assign_response.status_code == 200, assign_response.text
        assert "analyst" in assign_response.json()["roles"]

        remove_response = client.delete(
            f"/platform-admin/users/{target_id}/roles/analyst", headers=headers
        )
        assert remove_response.status_code == 200
        assert "analyst" not in remove_response.json()["roles"]

        audit_response = client.get(
            "/platform-admin/audit-logs",
            params={"resource_type": "user", "resource_id": target_id},
            headers=headers,
        )
        actions = [event["action"] for event in audit_response.json()]
        assert "role_assigned" in actions
        assert "role_removed" in actions

    def test_assigning_an_unknown_role_is_a_400(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform8@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            user = get_user_by_email(session, "platform8@tenant-a.example.com")
            assert user is not None
            user_id = str(user.id)
        finally:
            session.close()
        response = client.post(
            f"/platform-admin/users/{user_id}/roles",
            json={"role_name": "not-a-real-role"},
            headers=_headers(platform_tokens),
        )
        assert response.status_code == 400

    def test_assigning_a_role_to_an_unknown_user_is_404(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform9@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.post(
            f"/platform-admin/users/{uuid.uuid4()}/roles",
            json={"role_name": "analyst"},
            headers=_headers(platform_tokens),
        )
        assert response.status_code == 404


class TestRolesEndpoint:
    def test_lists_every_seeded_role_with_its_permissions(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform10@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.get("/platform-admin/roles", headers=_headers(platform_tokens))
        assert response.status_code == 200
        by_name = {role["name"]: role for role in response.json()}
        assert "platform_admin" in by_name
        assert "platform.admin" in by_name["platform_admin"]["permissions"]
        assert "admin" in by_name
        assert "platform.admin" not in by_name["admin"]["permissions"]


class TestConfigStatus:
    def test_only_boolean_flags_are_reported(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform11@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.get("/platform-admin/config-status", headers=_headers(platform_tokens))
        assert response.status_code == 200
        flags = response.json()["flags"]
        assert len(flags) > 0
        assert all(isinstance(value, bool) for value in flags.values())
        # Never leaks a secret field by name, even accidentally.
        assert "db_password" not in flags
        assert "jwt_secret_key" not in flags


class TestDatabasesNeverExposeCredentials:
    def test_no_credential_field_is_present(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform12@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.get("/platform-admin/databases", headers=_headers(platform_tokens))
        assert response.status_code == 200
        for entry in response.json():
            assert "db_user" not in entry
            assert "db_password" not in entry
            assert "db_connection_string" not in entry


class TestSecurityEventsRingBuffer:
    def test_a_logged_security_event_is_readable_back(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform13@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        log_security_event(
            "test_only_platform_admin_event", "warning", "A test-only event.", probe="yes"
        )
        response = client.get(
            "/platform-admin/security-events",
            params={"event_type": "test_only_platform_admin_event"},
            headers=_headers(platform_tokens),
        )
        assert response.status_code == 200
        events = response.json()
        assert len(events) >= 1
        assert events[0]["event_type"] == "test_only_platform_admin_event"
        assert events[0]["severity"] == "warning"


class TestSemanticReviewQueueAndJobs:
    def test_empty_platform_reports_zero_counts(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform14@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        headers = _headers(platform_tokens)

        queue_response = client.get("/platform-admin/semantic-review-queue", headers=headers)
        assert queue_response.status_code == 200
        assert queue_response.json()["total_pending"] == 0

        jobs_response = client.get("/platform-admin/jobs", headers=headers)
        assert jobs_response.status_code == 200
        assert jobs_response.json() == []


class TestMetrics:
    def test_merged_metrics_route_returns_a_well_formed_snapshot(self, client: TestClient):
        platform_tokens = _register_in_tenant(
            client, "platform15@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        response = client.get("/platform-admin/metrics", headers=_headers(platform_tokens))
        assert response.status_code == 200
        assert response.json()["tenant_id"] is None
