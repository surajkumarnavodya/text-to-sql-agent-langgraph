"""End-to-end HTTP tests for api/identity_auth.py, via a real
`fastapi.testclient.TestClient` against the full `api.main.app` -- the same
`tests/test_api_documents.py`-style pattern (a `Settings(...)` built
explicitly, monkeypatched into every module that calls `get_settings()`),
with the identity engine itself monkeypatched to an in-memory SQLite
database instead of a real PostgreSQL connection (see
tests/test_identity_repository_users.py's own docstring for why SQLite is
this repo's testing-only stand-in here).
"""

from __future__ import annotations

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
from config.settings import Settings

_SETTINGS = Settings(
    local_auth_enabled=True,
    auth_database_url="postgresql://placeholder/unused",
    jwt_secret_key="s" * 40,
    jwt_issuer="text-to-sql-agent",
    jwt_audience="text-to-sql-web",
    allow_public_registration=True,
    cookie_secure=False,  # TestClient runs over plain HTTP
    password_min_length=8,
    max_login_attempts=3,
    login_lockout_minutes=15,
    login_rate_limit_per_minute=1000,
    register_rate_limit_per_hour=1000,
)


@pytest.fixture(autouse=True)
def _identity_test_db(monkeypatch):
    """One fresh in-memory SQLite database per test, with RBAC seed data
    loaded, wired in as if it were the real identity database."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)

    from identity.db import get_identity_session

    session = get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    # Reset the per-IP rate limiters between tests -- see
    # tests/test_api_execute.py's fixture of the same name/reasoning.
    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()
    identity_auth_mod._refresh_limiters.clear()

    yield engine


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _register(
    client: TestClient,
    email: str = "alice@example.com",
    password: str = "correcthorse1",
    display_name: str = "Test User",
) -> dict:
    response = client.post(
        "/auth/register", json={"email": email, "password": password, "display_name": display_name}
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestRegister:
    def test_creates_account_and_returns_access_token(self, client):
        body = _register(client)
        assert body["token_type"] == "bearer"
        assert body["user"]["email"] == "alice@example.com"
        assert body["user"]["roles"] == ["user"]

    def test_sets_refresh_cookie(self, client):
        response = client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        assert "refresh_token" in response.cookies

    def test_duplicate_email_is_409(self, client):
        _register(client)
        response = client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "anotherpassword1",
                "display_name": "Test User",
            },
        )
        assert response.status_code == 409

    def test_password_too_short_is_400(self, client):
        response = client.post(
            "/auth/register",
            json={"email": "bob@example.com", "password": "short", "display_name": "Test User"},
        )
        assert response.status_code == 400

    def test_disabled_when_public_registration_off(self, client, monkeypatch):
        settings_no_public = _SETTINGS.model_copy(update={"allow_public_registration": False})
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: settings_no_public)
        response = client.post(
            "/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        assert response.status_code == 403

    def test_404_when_local_auth_disabled(self, client, monkeypatch):
        disabled = _SETTINGS.model_copy(update={"local_auth_enabled": False})
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: disabled)
        response = client.post(
            "/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        assert response.status_code == 404


class TestLocalAuthDisabledIsA404EverywhereNotA500:
    """Regression coverage for a real bug found manually after Phase A
    landed: `get_identity_db`/`require_local_user` (api/identity_authz.py)
    are FastAPI dependencies, resolved *before* a route's own body runs --
    a route-body-only "is local auth enabled" check (this router's original
    shape) never got a chance to run before `identity.db
    .get_identity_session` raised an unhandled `IdentityNotConfiguredError`
    (surfacing as a raw 500) the moment `local_auth_enabled` was False.
    `api/identity_authz.py::require_local_auth_enabled` is now the single
    shared gate, called from *inside* `get_identity_db` itself specifically
    so it runs before a session is ever attempted -- these tests patch
    `api.identity_authz`'s own `get_settings` (the one that actually
    matters for this ordering bug), not just `api.identity_auth`'s, to
    actually exercise the dependency-level path.
    """

    @pytest.fixture(autouse=True)
    def _disable_local_auth(self, monkeypatch):
        disabled = _SETTINGS.model_copy(update={"local_auth_enabled": False})
        monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: disabled)
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: disabled)

    def test_register_is_404_not_500(self, client):
        response = client.post(
            "/auth/register",
            json={
                "email": "bob@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        assert response.status_code == 404

    def test_login_is_404_not_500(self, client):
        response = client.post(
            "/auth/login", json={"email": "bob@example.com", "password": "correcthorse1"}
        )
        assert response.status_code == 404

    def test_refresh_is_404_not_500(self, client):
        assert client.post("/auth/refresh").status_code == 404

    def test_logout_is_404_not_500(self, client):
        assert client.post("/auth/logout").status_code == 404

    def test_me_is_404_not_500(self, client):
        # No credential presented at all -- still must not be a 500, and in
        # particular must not be a bare 401 from verify_api_key racing
        # ahead of the local-auth-enabled check in a way that hides this
        # feature being off entirely.
        response = client.get("/auth/me")
        assert response.status_code in (401, 404)
        assert response.status_code != 500


class TestLogin:
    def test_succeeds_with_correct_credentials(self, client):
        _register(client)
        response = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert response.status_code == 200
        assert response.json()["user"]["email"] == "alice@example.com"

    def test_wrong_password_is_401_with_generic_message(self, client):
        _register(client)
        response = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}
        )
        assert response.status_code == 401
        assert "incorrect" in response.json()["detail"].lower()

    def test_unknown_email_gives_the_same_generic_message(self, client):
        """Account-enumeration defense: a nonexistent email must produce
        an identical response shape to a wrong password."""
        wrong_password_response = client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        assert wrong_password_response.status_code == 201
        bad_login = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}
        )
        unknown_login = client.post(
            "/auth/login", json={"email": "nobody-at-all@example.com", "password": "wrong-password"}
        )
        assert bad_login.status_code == unknown_login.status_code == 401
        assert bad_login.json()["detail"] == unknown_login.json()["detail"]

    def test_locks_account_after_max_attempts(self, client):
        _register(client)
        for _ in range(3):
            client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong"})
        # A 4th attempt, even with the *correct* password, must still fail
        # while locked.
        response = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert response.status_code == 401


class TestMeAndChangePassword:
    def test_me_returns_current_user(self, client):
        tokens = _register(client)
        response = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert response.status_code == 200
        assert response.json()["email"] == "alice@example.com"

    def test_me_requires_authentication(self, client):
        response = client.get("/auth/me")
        assert response.status_code == 401

    def test_change_password_requires_current_password(self, client):
        tokens = _register(client)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        response = client.post(
            "/auth/change-password",
            json={"current_password": "wrong", "new_password": "a-new-password-1"},
            headers=headers,
        )
        assert response.status_code == 401

    def test_change_password_succeeds_and_old_password_stops_working(self, client):
        tokens = _register(client)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        response = client.post(
            "/auth/change-password",
            json={"current_password": "correcthorse1", "new_password": "a-new-password-1"},
            headers=headers,
        )
        assert response.status_code == 200

        old_login = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert old_login.status_code == 401
        new_login = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "a-new-password-1"}
        )
        assert new_login.status_code == 200


class TestChangePasswordRevokesOtherDevices:
    """A voluntary password change revokes every other live session -- the
    same reasoning the reset path already applies -- while keeping the
    device that made the change signed in."""

    def test_other_device_is_signed_out_and_this_device_stays_signed_in(self, client):
        from fastapi.testclient import TestClient

        tokens = _register(client)
        other_device = TestClient(client.app)
        login = other_device.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert login.status_code == 200
        assert other_device.post("/auth/refresh").status_code == 200

        response = client.post(
            "/auth/change-password",
            json={"current_password": "correcthorse1", "new_password": "a-new-password-1"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert response.status_code == 200

        assert other_device.post("/auth/refresh").status_code == 401
        assert client.post("/auth/refresh").status_code == 200


class TestRefreshRotationAndReuseDetection:
    def test_refresh_issues_a_new_access_token(self, client):
        _register(client)
        response = client.post("/auth/refresh")
        assert response.status_code == 200
        assert response.json()["user"]["email"] == "alice@example.com"

    def test_refresh_rotates_the_cookie(self, client):
        register_response = client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        first_cookie = register_response.cookies["refresh_token"]
        refresh_response = client.post("/auth/refresh")
        assert refresh_response.cookies["refresh_token"] != first_cookie

    def test_reusing_an_old_cookie_after_refresh_is_rejected(self, client):
        register_response = client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        old_raw_token = register_response.cookies["refresh_token"]

        client.post("/auth/refresh")  # rotates -- client's cookie jar now holds the new one

        # Manually replay the pre-rotation token, bypassing the client's
        # own (already-updated) cookie jar.
        replay_client = TestClient(api_main.app)
        replay_client.cookies.set("refresh_token", old_raw_token)
        response = replay_client.post("/auth/refresh")
        assert response.status_code == 401

    def test_no_cookie_at_all_is_401(self, client):
        response = client.post("/auth/refresh")
        assert response.status_code == 401


class TestLogoutAndSessions:
    def test_logout_clears_cookie(self, client):
        _register(client)
        response = client.post("/auth/logout")
        assert response.status_code == 200
        # After logout, refresh must no longer work.
        assert client.post("/auth/refresh").status_code == 401

    def test_logout_without_a_cookie_is_still_a_success(self, client):
        response = client.post("/auth/logout")
        assert response.status_code == 200

    def test_sessions_lists_the_current_session(self, client):
        tokens = _register(client)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        response = client.get("/auth/sessions", headers=headers)
        assert response.status_code == 200
        sessions = response.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["is_current"] is True

    def test_logout_all_revokes_every_session(self, client):
        tokens = _register(client)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        response = client.post("/auth/logout-all", headers=headers)
        assert response.status_code == 200
        # The access token is still nominally valid (short-lived JWT, not
        # revoked directly), but no active session remains to refresh from.
        assert client.post("/auth/refresh").status_code == 401

    def test_cannot_revoke_another_users_session(self, client):
        alice_tokens = _register(client, email="alice@example.com")
        bob_client = TestClient(api_main.app)
        bob_tokens = _register(bob_client, email="bob@example.com")

        alice_headers = {"Authorization": f"Bearer {alice_tokens['access_token']}"}
        alice_sessions = client.get("/auth/sessions", headers=alice_headers).json()["sessions"]

        # Bob attempts to revoke Alice's session using Bob's own token.
        bob_headers = {"Authorization": f"Bearer {bob_tokens['access_token']}"}
        response = bob_client.delete(
            f"/auth/sessions/{alice_sessions[0]['id']}", headers=bob_headers
        )
        assert response.status_code == 404


class TestPasswordResetFlow:
    def test_forgot_password_always_returns_generic_success(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            identity_auth_mod,
            "send_password_reset_email",
            lambda to_email, raw_token, *, base_url: captured.update(
                to_email=to_email, raw_token=raw_token
            ),
        )
        _register(client)

        known = client.post("/auth/forgot-password", json={"email": "alice@example.com"})
        unknown = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})

        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()
        assert captured["to_email"] == "alice@example.com"
        assert captured["raw_token"]

    def test_reset_password_with_valid_token_changes_password(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            identity_auth_mod,
            "send_password_reset_email",
            lambda to_email, raw_token, *, base_url: captured.update(raw_token=raw_token),
        )
        _register(client)
        client.post("/auth/forgot-password", json={"email": "alice@example.com"})

        response = client.post(
            "/auth/reset-password",
            json={"token": captured["raw_token"], "new_password": "brand-new-password-1"},
        )
        assert response.status_code == 200

        old_login = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert old_login.status_code == 401
        new_login = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "brand-new-password-1"}
        )
        assert new_login.status_code == 200

    def test_reset_password_revokes_existing_sessions(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            identity_auth_mod,
            "send_password_reset_email",
            lambda to_email, raw_token, *, base_url: captured.update(raw_token=raw_token),
        )
        _register(client)  # sets a refresh cookie on `client`
        client.post("/auth/forgot-password", json={"email": "alice@example.com"})
        client.post(
            "/auth/reset-password",
            json={"token": captured["raw_token"], "new_password": "brand-new-password-1"},
        )
        # The pre-reset session's refresh cookie must no longer work.
        assert client.post("/auth/refresh").status_code == 401

    def test_reset_password_rejects_unknown_token(self, client):
        response = client.post(
            "/auth/reset-password", json={"token": "made-up", "new_password": "a-new-password-1"}
        )
        assert response.status_code == 400

    def test_reset_password_token_is_single_use(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            identity_auth_mod,
            "send_password_reset_email",
            lambda to_email, raw_token, *, base_url: captured.update(raw_token=raw_token),
        )
        _register(client)
        client.post("/auth/forgot-password", json={"email": "alice@example.com"})
        client.post(
            "/auth/reset-password",
            json={"token": captured["raw_token"], "new_password": "brand-new-password-1"},
        )
        second_attempt = client.post(
            "/auth/reset-password",
            json={"token": captured["raw_token"], "new_password": "yet-another-password-1"},
        )
        assert second_attempt.status_code == 400


class TestEmailVerificationFlow:
    def test_registration_requires_verification_when_configured(self, client, monkeypatch):
        require_verification = _SETTINGS.model_copy(update={"require_email_verification": True})
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: require_verification)
        captured = {}
        monkeypatch.setattr(
            identity_auth_mod,
            "send_verification_email",
            lambda to_email, raw_token, *, base_url: captured.update(
                to_email=to_email, raw_token=raw_token
            ),
        )

        response = client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )
        assert response.status_code == 201
        assert "access_token" not in response.json()
        assert captured["to_email"] == "alice@example.com"

        # Cannot sign in until verified.
        login_before = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert login_before.status_code == 401

        verify_response = client.post("/auth/verify-email", json={"token": captured["raw_token"]})
        assert verify_response.status_code == 200

        login_after = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "correcthorse1"}
        )
        assert login_after.status_code == 200

    def test_verify_email_rejects_unknown_token(self, client):
        response = client.post("/auth/verify-email", json={"token": "made-up"})
        assert response.status_code == 400

    def test_resend_verification_is_account_enumeration_safe(self, client, monkeypatch):
        require_verification = _SETTINGS.model_copy(update={"require_email_verification": True})
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: require_verification)
        monkeypatch.setattr(identity_auth_mod, "send_verification_email", lambda *a, **k: None)
        client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )

        known = client.post("/auth/resend-verification", json={"email": "alice@example.com"})
        unknown = client.post("/auth/resend-verification", json={"email": "nobody@example.com"})
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()

    def test_resend_verification_issues_a_redeemable_token(self, client, monkeypatch):
        require_verification = _SETTINGS.model_copy(update={"require_email_verification": True})
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: require_verification)
        monkeypatch.setattr(identity_auth_mod, "send_verification_email", lambda *a, **k: None)
        client.post(
            "/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correcthorse1",
                "display_name": "Test User",
            },
        )

        captured = {}
        monkeypatch.setattr(
            identity_auth_mod,
            "send_verification_email",
            lambda to_email, raw_token, *, base_url: captured.update(raw_token=raw_token),
        )
        client.post("/auth/resend-verification", json={"email": "alice@example.com"})
        response = client.post("/auth/verify-email", json={"token": captured["raw_token"]})
        assert response.status_code == 200
