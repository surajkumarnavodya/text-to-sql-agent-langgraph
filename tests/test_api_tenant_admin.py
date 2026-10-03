"""End-to-end HTTP tests for api/tenant_admin.py (Prompt 29,
`29_TENANT_ADMIN_DASHBOARD_CONTRACT.md`) -- same `TestClient` +
in-memory-SQLite-identity-DB pattern as `tests/test_api_platform_admin.py`.

**This module's own entire reason to exist is the opposite of
`test_api_platform_admin.py`'s**: every route must prove it can *only*
ever see/act on the caller's own tenant, never another one and never a
platform-wide view. Two real tenants (`tenant-a`/`tenant-b`), each with
their own account and their own configured database connection
(`DB_<NAME>_TENANT_IDS`-style binding, expressed directly via
`DatabaseConnectionConfig.tenant_ids`), are created so cross-tenant
leakage would be directly observable rather than merely assumed absent.
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
import api.tenant_admin as tenant_admin_mod
from config.settings import DatabaseConnectionConfig, Settings
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
    databases=(
        DatabaseConnectionConfig(name="db-a", db_type="postgresql", tenant_ids=("tenant-a",)),
        DatabaseConnectionConfig(name="db-b", db_type="postgresql", tenant_ids=("tenant-b",)),
        DatabaseConnectionConfig(name="db-shared", db_type="postgresql"),
    ),
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
    monkeypatch.setattr(tenant_admin_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(
        tenant_admin_mod,
        "test_connection",
        lambda config: ConnectionTestResult(success=True, message="ok"),
    )

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


class TestTenantIsolation:
    """The prompt's own explicit testing requirement: a tenant admin must
    never see another tenant's data, and never a platform-wide view."""

    def test_profile_returns_only_the_callers_own_tenant(self, client: TestClient):
        admin_a = _register_in_tenant(client, "admin@tenant-a.example.com", "admin", "tenant-a")
        response = client.get("/tenant-admin/profile", headers=_headers(admin_a))
        assert response.status_code == 200
        assert response.json()["id"] == "tenant-a"

    def test_databases_list_excludes_the_other_tenants_bound_connection(self, client: TestClient):
        admin_a = _register_in_tenant(client, "admin2@tenant-a.example.com", "admin", "tenant-a")
        response = client.get("/tenant-admin/databases", headers=_headers(admin_a))
        assert response.status_code == 200
        names = {database["name"] for database in response.json()}
        assert names == {"db-a", "db-shared"}
        assert "db-b" not in names

    def test_users_list_never_includes_the_other_tenants_accounts(self, client: TestClient):
        admin_a = _register_in_tenant(client, "admin3@tenant-a.example.com", "admin", "tenant-a")
        _register_in_tenant(client, "other@tenant-b.example.com", "user", "tenant-b")

        response = client.get("/tenant-admin/users", headers=_headers(admin_a))
        assert response.status_code == 200
        tenant_ids_seen = {user["tenant_id"] for user in response.json()}
        assert tenant_ids_seen == {"tenant-a"}

    def test_cannot_assign_a_role_to_an_account_in_another_tenant(self, client: TestClient):
        admin_a = _register_in_tenant(client, "admin4@tenant-a.example.com", "admin", "tenant-a")
        _register_in_tenant(client, "target@tenant-b.example.com", "user", "tenant-b")
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            target = get_user_by_email(session, "target@tenant-b.example.com")
            assert target is not None
            target_id = str(target.id)
        finally:
            session.close()

        response = client.post(
            f"/tenant-admin/users/{target_id}/roles",
            json={"role_name": "analyst"},
            headers=_headers(admin_a),
        )
        # Same anti-enumeration posture as api/semantic_catalog.py/api/onboarding.py:
        # a cross-tenant target looks exactly like a nonexistent one.
        assert response.status_code == 404

    def test_security_events_are_filtered_to_the_callers_own_tenant(self, client: TestClient):
        from security.audit_log import (
            log_security_event,
            reset_audit_tenant_id,
            set_audit_tenant_id,
        )

        admin_a = _register_in_tenant(client, "admin5@tenant-a.example.com", "admin", "tenant-a")

        # `log_security_event` reads the current *contextvar* for its
        # `tenant_id`, not a kwarg -- mirrors how `agent.graph.run_agent`
        # actually binds it per real request (`set_audit_tenant_id`).
        token_a = set_audit_tenant_id("tenant-a")
        try:
            log_security_event("probe_event", "info", "probe")
        finally:
            reset_audit_tenant_id(token_a)
        token_b = set_audit_tenant_id("tenant-b")
        try:
            log_security_event("probe_event", "info", "probe")
        finally:
            reset_audit_tenant_id(token_b)

        response = client.get("/tenant-admin/audit", headers=_headers(admin_a))
        assert response.status_code == 200
        events = [event for event in response.json() if event["event_type"] == "probe_event"]
        assert len(events) == 1
        assert events[0]["tenant_id"] == "tenant-a"

    def test_no_route_accepts_a_tenant_id_override(self, client: TestClient):
        """There is structurally no `tenant_id` parameter on any route in
        this router (see api/tenant_admin.py's own docstring) -- this
        proves a client-supplied one is simply ignored, not honored."""
        admin_a = _register_in_tenant(client, "admin6@tenant-a.example.com", "admin", "tenant-a")
        response = client.get(
            "/tenant-admin/profile",
            params={"tenant_id": "tenant-b"},
            headers=_headers(admin_a),
        )
        assert response.status_code == 200
        assert response.json()["id"] == "tenant-a"


class TestRoleSpecificAccess:
    def test_a_plain_user_cannot_see_the_dashboard(self, client: TestClient):
        user = _register_in_tenant(client, "user@tenant-a.example.com", "user", "tenant-a")
        response = client.get("/tenant-admin/profile", headers=_headers(user))
        assert response.status_code == 403

    def test_an_analyst_cannot_see_the_dashboard(self, client: TestClient):
        analyst = _register_in_tenant(client, "analyst@tenant-a.example.com", "analyst", "tenant-a")
        response = client.get("/tenant-admin/profile", headers=_headers(analyst))
        assert response.status_code == 403

    def test_an_auditor_can_view_but_not_assign_roles(self, client: TestClient):
        auditor = _register_in_tenant(client, "auditor@tenant-a.example.com", "auditor", "tenant-a")
        view_response = client.get("/tenant-admin/users", headers=_headers(auditor))
        assert view_response.status_code == 200

        other = _register_in_tenant(client, "other2@tenant-a.example.com", "user", "tenant-a")
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            target = get_user_by_email(session, "other2@tenant-a.example.com")
            assert target is not None
            target_id = str(target.id)
        finally:
            session.close()
        del other

        assign_response = client.post(
            f"/tenant-admin/users/{target_id}/roles",
            json={"role_name": "analyst"},
            headers=_headers(auditor),
        )
        assert assign_response.status_code == 403

    def test_an_admin_can_assign_and_remove_a_role(self, client: TestClient):
        admin = _register_in_tenant(client, "admin7@tenant-a.example.com", "admin", "tenant-a")
        _register_in_tenant(client, "member@tenant-a.example.com", "user", "tenant-a")
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            target = get_user_by_email(session, "member@tenant-a.example.com")
            assert target is not None
            target_id = str(target.id)
        finally:
            session.close()

        assign_response = client.post(
            f"/tenant-admin/users/{target_id}/roles",
            json={"role_name": "analyst"},
            headers=_headers(admin),
        )
        assert assign_response.status_code == 200, assign_response.text
        assert "analyst" in assign_response.json()["roles"]

        remove_response = client.delete(
            f"/tenant-admin/users/{target_id}/roles/analyst", headers=_headers(admin)
        )
        assert remove_response.status_code == 200
        assert "analyst" not in remove_response.json()["roles"]

    def test_the_platform_admin_role_can_never_be_assigned_from_here(self, client: TestClient):
        admin = _register_in_tenant(client, "admin8@tenant-a.example.com", "admin", "tenant-a")
        _register_in_tenant(client, "member2@tenant-a.example.com", "user", "tenant-a")
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            target = get_user_by_email(session, "member2@tenant-a.example.com")
            assert target is not None
            target_id = str(target.id)
        finally:
            session.close()

        response = client.post(
            f"/tenant-admin/users/{target_id}/roles",
            json={"role_name": "platform_admin"},
            headers=_headers(admin),
        )
        assert response.status_code == 403

    def test_the_platform_admin_role_is_excluded_from_the_roles_list(self, client: TestClient):
        admin = _register_in_tenant(client, "admin9@tenant-a.example.com", "admin", "tenant-a")
        response = client.get("/tenant-admin/roles", headers=_headers(admin))
        assert response.status_code == 200
        names = {role["name"] for role in response.json()}
        assert "platform_admin" not in names
        assert "admin" in names


class TestSemanticAndReviewSections:
    def test_empty_tenant_reports_zero_counts(self, client: TestClient):
        admin = _register_in_tenant(client, "admin10@tenant-a.example.com", "admin", "tenant-a")
        headers = _headers(admin)

        catalog_response = client.get("/tenant-admin/semantic-catalog-status", headers=headers)
        assert catalog_response.status_code == 200
        assert catalog_response.json()["draft_count"] == 0

        pending_response = client.get("/tenant-admin/pending-reviews", headers=headers)
        assert pending_response.status_code == 200
        assert pending_response.json()["total_pending"] == 0

        golden_response = client.get("/tenant-admin/golden-questions", headers=headers)
        assert golden_response.status_code == 200
        assert golden_response.json() == []

        evaluation_response = client.get("/tenant-admin/evaluation", headers=headers)
        assert evaluation_response.status_code == 200
        assert evaluation_response.json() == []


class TestSchemaRefreshIsTenantScoped:
    def test_refresh_only_touches_the_callers_own_databases(self, client: TestClient, monkeypatch):
        admin_a = _register_in_tenant(client, "admin11@tenant-a.example.com", "admin", "tenant-a")

        refreshed_databases: list[str] = []

        def _fake_refresh(engine, db_name, settings=None, force=False):
            refreshed_databases.append(db_name)
            return []

        monkeypatch.setattr(tenant_admin_mod, "refresh_schema_index", _fake_refresh)
        monkeypatch.setattr(tenant_admin_mod, "get_read_only_engine", lambda config: object())
        monkeypatch.setattr(
            tenant_admin_mod, "get_last_discovery_diff", lambda name, settings: None
        )

        response = client.post("/tenant-admin/databases/refresh", headers=_headers(admin_a))
        assert response.status_code == 200, response.text
        assert set(refreshed_databases) == {"db-a", "db-shared"}
        assert "db-b" not in refreshed_databases

    def test_an_analyst_cannot_trigger_a_refresh(self, client: TestClient):
        analyst = _register_in_tenant(
            client, "analyst2@tenant-a.example.com", "analyst", "tenant-a"
        )
        response = client.post("/tenant-admin/databases/refresh", headers=_headers(analyst))
        assert response.status_code == 403


class TestUnauthenticated:
    def test_every_route_requires_authentication(self, client: TestClient):
        response = client.get("/tenant-admin/profile")
        assert response.status_code in (401, 403)


class TestSuspendedTenantLosesAccessImmediately:
    """`security/tenancy.py` rule 3 and `identity/repositories/tenants.py`
    both promise that suspending a tenant takes effect on the very next
    request across every tenant-scoped route. Before this regression test
    existed, `resolve_tenant_context` only ran at login/refresh -- a
    still-valid access token kept working on every identity-backed route
    for up to its full lifetime."""

    def test_suspended_tenant_user_is_refused_on_the_next_request(self, client: TestClient):
        from identity.repositories.tenants import set_tenant_status

        admin_a = _register_in_tenant(
            client, "suspend-me@tenant-a.example.com", "admin", "tenant-a"
        )
        assert client.get("/auth/me", headers=_headers(admin_a)).status_code == 200

        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            set_tenant_status(session, tenant_id="tenant-a", status="suspended")
        finally:
            session.close()

        assert client.get("/auth/me", headers=_headers(admin_a)).status_code == 401
        assert client.get("/tenant-admin/profile", headers=_headers(admin_a)).status_code == 401

    def test_other_tenants_are_unaffected_by_a_suspension(self, client: TestClient):
        from identity.repositories.tenants import set_tenant_status

        admin_b = _register_in_tenant(client, "stays-up@tenant-b.example.com", "admin", "tenant-b")
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            set_tenant_status(session, tenant_id="tenant-a", status="suspended")
        finally:
            session.close()

        assert client.get("/auth/me", headers=_headers(admin_b)).status_code == 200


class TestPlatformAdminRoleIsProtectedOnRemoval:
    """A tenant admin may not *assign* `platform_admin` (already enforced),
    and must not be able to *remove* it from a same-tenant account either --
    otherwise a tenant admin could strip a platform operator's cross-tenant
    access, a tenant-scoped action with a cross-tenant effect."""

    def test_tenant_admin_cannot_remove_platform_admin_from_a_same_tenant_user(
        self, client: TestClient
    ):
        from identity.repositories.users import get_user_roles

        tenant_admin = _register_in_tenant(
            client, "pa-guard-admin@tenant-a.example.com", "admin", "tenant-a"
        )
        operator = _register_in_tenant(
            client, "pa-guard-op@tenant-a.example.com", "platform_admin", "tenant-a"
        )
        operator_id = client.get("/auth/me", headers=_headers(operator)).json()["id"]

        response = client.delete(
            f"/tenant-admin/users/{operator_id}/roles/platform_admin",
            headers=_headers(tenant_admin),
        )
        assert response.status_code == 403

        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            assert "platform_admin" in get_user_roles(session, uuid.UUID(operator_id))
        finally:
            session.close()


class TestSchemaRefreshIsolatesDatabaseFailures:
    """One unreachable database must not abort the refresh of the tenant's
    other databases, and must not leak the raw driver error to the caller."""

    def test_a_failing_database_is_reported_and_the_rest_still_refresh(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        admin_a = _register_in_tenant(
            client, "refresh-iso@tenant-a.example.com", "admin", "tenant-a"
        )

        def fake_refresh(engine, db_name, settings=None, force=False):
            if db_name == "db-a":
                raise RuntimeError("could not connect to 10.0.0.5 password=hunter2")
            return []

        monkeypatch.setattr(tenant_admin_mod, "refresh_schema_index", fake_refresh)
        monkeypatch.setattr(tenant_admin_mod, "get_read_only_engine", lambda config: object())

        response = client.post("/tenant-admin/databases/refresh", headers=_headers(admin_a))
        assert response.status_code == 200, response.text
        by_name = {entry["database"]: entry for entry in response.json()["databases"]}
        assert set(by_name) == {"db-a", "db-shared"}
        assert by_name["db-shared"]["error"] is None
        assert by_name["db-a"]["error"]
        assert "hunter2" not in response.text
        assert "10.0.0.5" not in response.text
