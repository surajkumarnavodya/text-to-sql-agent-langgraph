"""HTTP tests for `api/navigation.py` -- Prompt 32.

Three things are checked end to end, with real local accounts, real tenants
and a real in-memory identity database (no mock of the authorization path):

1. **Navigation matches access.** For each persona, every screen `GET
   /navigation` returns has a representative API call that is *not* refused
   (403), and every screen it hides has one that *is* refused. Navigation
   can therefore never offer a screen whose data calls would be denied, and
   never hide one the caller may use.
2. **Unauthorized access is refused server-side,** not merely hidden.
3. **Tenant context is enforced.** A suspended tenant loses navigation on its
   next request, and the denial-report endpoint audits only real screen
   paths.

Representative endpoints use a status check (`!= 403`), not an exact body,
so these tests stay about authorization and do not depend on each endpoint's
data state.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base
from identity.repositories.tenants import create_tenant, set_tenant_status
from identity.repositories.users import assign_role, create_user
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.navigation as navigation_mod
import api.rate_limit as api_rate_limit_mod
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

#: Screen id -> one representative API call that screen's data depends on.
#: `chat` has no cheap read-only representative (its real call is a question
#: that reaches the LLM), so it is checked by navigation and the `ask`
#: capability alone, in the policy tests.
_REPRESENTATIVE: dict[str, tuple[str, str]] = {
    "knowledge_sources": ("GET", "/documents"),
    "recommendations": ("GET", "/recommendations"),
    "db_onboarding": ("GET", "/onboarding/jobs"),
    "semantic_review": ("GET", "/semantic-catalog/entries"),
    "tenant_admin": ("GET", "/tenant-admin/profile"),
    "platform_admin": ("GET", "/platform-admin/tenants"),
}

#: The persona -> role(s) mapping, mirrored from `tests/test_security_navigation.py`.
#: The platform operator also holds `admin`: `platform_admin` alone carries no
#: `agent.authz` base grants, so it cannot use the AI workspace (see that test
#: module's docstring and the contract doc). Operators are expected to assign both.
_PERSONAS: dict[str, tuple[str, ...]] = {
    "platform_super_admin": ("platform_admin", "admin"),
    "tenant_admin": ("admin",),
    "sme_reviewer": ("analyst",),
    "business_user": ("user",),
    "read_only_viewer": ("auditor",),
}


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
    monkeypatch.setattr(navigation_mod, "get_settings", lambda: _SETTINGS)

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


def _account(email: str, roles: str | tuple[str, ...], tenant_id: str = "tenant-a") -> str:
    """Creates a local account holding exactly `roles` (one role name, or a
    tuple of them), and returns its email."""
    role_names = (roles,) if isinstance(roles, str) else roles
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        user = create_user(
            session, email=email, password=PASSWORD, tenant_id=tenant_id, role_name=role_names[0]
        )
        for extra in role_names[1:]:
            assign_role(session, user_id=user.id, role_name=extra, assigned_by_user_id=None)
        session.commit()
        return email
    finally:
        session.close()


def _login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _navigation(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get("/navigation", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _screens(body: dict) -> set[str]:
    return {item["id"] for item in body["items"]}


class TestNavigationEndpoint:
    @pytest.mark.parametrize(
        ("persona", "expected"),
        [
            (
                "platform_super_admin",
                {
                    "chat",
                    "knowledge_sources",
                    "media_search",
                    "recommendations",
                    "db_onboarding",
                    "semantic_review",
                    "tenant_admin",
                    "platform_admin",
                },
            ),
            (
                "tenant_admin",
                {
                    "chat",
                    "knowledge_sources",
                    "media_search",
                    "recommendations",
                    "db_onboarding",
                    "semantic_review",
                    "tenant_admin",
                },
            ),
            (
                "sme_reviewer",
                {
                    "chat",
                    "knowledge_sources",
                    "media_search",
                    "recommendations",
                    "db_onboarding",
                    "semantic_review",
                },
            ),
            ("business_user", {"chat", "knowledge_sources", "media_search"}),
            ("read_only_viewer", {"tenant_admin"}),
        ],
    )
    def test_each_persona_gets_exactly_its_screens(self, client, persona, expected):
        email = _account(f"{persona}@tenant-a.example.com", _PERSONAS[persona])
        body = _navigation(client, _login(client, email))
        assert _screens(body) == expected
        assert body["tenant_id"] == "tenant-a"
        assert sorted(body["roles"]) == sorted(_PERSONAS[persona])

    def test_capabilities_are_returned_for_every_name(self, client):
        email = _account("cap@tenant-a.example.com", "user")
        body = _navigation(client, _login(client, email))
        assert body["capabilities"]["ask"] is True
        assert body["capabilities"]["platform_admin"] is False
        assert body["capabilities"]["manage_catalog"] is False

    def test_an_unauthenticated_request_is_refused(self, client):
        assert client.get("/navigation").status_code == 401


class TestNavigationMatchesAccess:
    @pytest.mark.parametrize("persona", list(_PERSONAS))
    def test_every_visible_screen_is_reachable_and_every_hidden_one_is_refused(
        self, client, persona
    ):
        email = _account(f"match-{persona}@tenant-a.example.com", _PERSONAS[persona])
        headers = _login(client, email)
        visible = _screens(_navigation(client, headers))

        for screen, (method, path) in _REPRESENTATIVE.items():
            response = client.request(method, path, headers=headers)
            if screen in visible:
                assert response.status_code != 403, f"{persona} sees {screen} but {path} refused"
            else:
                assert response.status_code == 403, f"{persona} hidden {screen} but {path} allowed"


class TestUnauthorizedDirectAccess:
    def test_a_business_user_cannot_open_the_platform_admin_api(self, client):
        headers = _login(client, _account("bu@tenant-a.example.com", "user"))
        assert client.get("/platform-admin/tenants", headers=headers).status_code == 403

    def test_a_tenant_admin_cannot_open_the_platform_admin_api(self, client):
        headers = _login(client, _account("ta@tenant-a.example.com", "admin"))
        assert client.get("/platform-admin/tenants", headers=headers).status_code == 403

    def test_a_read_only_viewer_cannot_review_recommendations(self, client):
        headers = _login(client, _account("ro@tenant-a.example.com", "auditor"))
        assert client.get("/recommendations", headers=headers).status_code == 403


class TestTenantContext:
    def test_a_suspended_tenant_loses_navigation_on_its_next_request(self, client):
        email = _account("sus@tenant-a.example.com", "admin")
        headers = _login(client, email)
        assert _navigation(client, headers)["tenant_id"] == "tenant-a"

        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            set_tenant_status(session, tenant_id="tenant-a", status="suspended")
        finally:
            session.close()

        assert client.get("/navigation", headers=headers).status_code == 401

    def test_each_tenant_sees_its_own_tenant_id_only(self, client):
        headers_a = _login(client, _account("ta1@tenant-a.example.com", "admin"))
        headers_b = _login(
            client, _account("tb1@tenant-b.example.com", "admin", tenant_id="tenant-b")
        )
        assert _navigation(client, headers_a)["tenant_id"] == "tenant-a"
        assert _navigation(client, headers_b)["tenant_id"] == "tenant-b"


class TestAccessDeniedReport:
    def test_a_real_screen_path_is_audited(self, client, caplog):
        headers = _login(client, _account("rep@tenant-a.example.com", "user"))
        with caplog.at_level(logging.WARNING):
            response = client.post(
                "/navigation/access-denied", json={"path": "/platform-admin"}, headers=headers
            )
        assert response.status_code == 204
        assert any(
            "event=ui_route_denied" in r.getMessage()
            and "screen='platform_admin'" in r.getMessage()
            for r in caplog.records
        )

    def test_an_arbitrary_path_is_answered_but_not_logged(self, client, caplog):
        """A caller cannot use this endpoint to write free text into the audit
        log: an unrecognized path is answered identically and not logged."""
        headers = _login(client, _account("junk@tenant-a.example.com", "user"))
        with caplog.at_level(logging.WARNING):
            response = client.post(
                "/navigation/access-denied",
                json={"path": "/anything-<script>-at-all"},
                headers=headers,
            )
        assert response.status_code == 204
        assert not any("event=ui_route_denied" in r.getMessage() for r in caplog.records)

    def test_an_unknown_field_is_rejected(self, client):
        headers = _login(client, _account("extra@tenant-a.example.com", "user"))
        response = client.post(
            "/navigation/access-denied",
            json={"path": "/", "detail": "x"},
            headers=headers,
        )
        assert response.status_code == 422

    def test_an_unauthenticated_report_is_refused(self, client):
        response = client.post("/navigation/access-denied", json={"path": "/"})
        assert response.status_code == 401


def test_the_navigation_routes_are_registered_on_the_app():
    paths = set(api_main.app.openapi()["paths"])
    assert "/navigation" in paths
    assert "/navigation/access-denied" in paths
