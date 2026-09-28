"""End-to-end HTTP tests for the Google sign-in routes in
api/identity_auth.py (`GET /auth/google/nonce`, `POST /auth/google`,
`GET/POST/DELETE /auth/google/link`) -- same `fastapi.testclient.TestClient`
+ in-memory-SQLite-identity-database pattern as
tests/test_api_identity_auth.py, with `security.google_oidc
.verify_google_id_token` mocked (there is no way to produce a token Google's
own library will accept without a real Google account completing a real
sign-in -- see docs/AUTHENTICATION.md's own disclosure on what is and
isn't live-verified).
"""

from __future__ import annotations

from unittest.mock import patch

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
from security.google_oidc import GoogleIdentityClaims, GoogleTokenValidationError

_SETTINGS = Settings(
    local_auth_enabled=True,
    auth_database_url="postgresql://placeholder/unused",
    jwt_secret_key="s" * 40,
    jwt_issuer="text-to-sql-agent",
    jwt_audience="text-to-sql-web",
    allow_public_registration=True,
    cookie_secure=False,
    password_min_length=8,
    max_login_attempts=3,
    login_lockout_minutes=15,
    login_rate_limit_per_minute=1000,
    register_rate_limit_per_hour=1000,
    google_oauth_client_id="test-client-id.apps.googleusercontent.com",
)

_UNCONFIGURED_SETTINGS = Settings(**{**_SETTINGS.__dict__, "google_oauth_client_id": None})


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

    from identity.db import get_identity_session

    session = get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()
    identity_auth_mod._refresh_limiters.clear()
    identity_auth_mod._google_signin_limiters.clear()
    identity_auth_mod._google_nonce_limiters.clear()

    yield engine


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _fake_claims(
    *,
    sub: str = "110044332232098765432",
    email: str = "googleuser@example.com",
    email_verified: bool = True,
    name: str | None = "Google User",
    hd: str | None = None,
) -> GoogleIdentityClaims:
    return GoogleIdentityClaims(
        sub=sub, email=email, email_verified=email_verified, name=name, hd=hd
    )


class TestGoogleNonce:
    def test_returns_a_nonce_when_configured(self, client: TestClient):
        response = client.get("/auth/google/nonce")
        assert response.status_code == 200
        assert len(response.json()["nonce"]) >= 32

    def test_404_when_google_signin_not_configured(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _UNCONFIGURED_SETTINGS)
        response = client.get("/auth/google/nonce")
        assert response.status_code == 404


class TestGoogleSignIn:
    def test_new_user_is_created_and_signed_in(self, client: TestClient):
        with patch.object(identity_auth_mod, "verify_google_id_token", return_value=_fake_claims()):
            response = client.post("/auth/google", json={"credential": "fake-credential"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["email"] == "googleuser@example.com"
        assert "access_token" in body
        assert response.cookies.get("refresh_token") is not None

    def test_returning_user_with_same_sub_gets_the_same_account(self, client: TestClient):
        with patch.object(identity_auth_mod, "verify_google_id_token", return_value=_fake_claims()):
            first = client.post("/auth/google", json={"credential": "fake-credential-1"})
            second = client.post("/auth/google", json={"credential": "fake-credential-2"})
        assert first.json()["user"]["id"] == second.json()["user"]["id"]

    def test_verified_email_conflict_with_existing_local_account_returns_409(
        self, client: TestClient
    ):
        register = client.post(
            "/auth/register",
            json={
                "email": "existing@example.com",
                "password": "correcthorse1",
                "display_name": "Existing User",
            },
        )
        assert register.status_code == 201

        with patch.object(
            identity_auth_mod,
            "verify_google_id_token",
            return_value=_fake_claims(email="existing@example.com", email_verified=True),
        ):
            response = client.post("/auth/google", json={"credential": "fake-credential"})
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_unverified_email_collision_returns_generic_401_not_409(self, client: TestClient):
        register = client.post(
            "/auth/register",
            json={
                "email": "existing2@example.com",
                "password": "correcthorse1",
                "display_name": "Existing User",
            },
        )
        assert register.status_code == 201

        with patch.object(
            identity_auth_mod,
            "verify_google_id_token",
            return_value=_fake_claims(email="existing2@example.com", email_verified=False),
        ):
            response = client.post("/auth/google", json={"credential": "fake-credential"})
        assert response.status_code == 401
        assert "already exists" not in response.json()["detail"]

    def test_invalid_credential_is_rejected_with_a_safe_generic_message(self, client: TestClient):
        with patch.object(
            identity_auth_mod,
            "verify_google_id_token",
            side_effect=GoogleTokenValidationError("Google sign-in verification failed."),
        ):
            response = client.post("/auth/google", json={"credential": "garbage"})
        assert response.status_code == 401
        assert response.json()["detail"] == "Google sign-in verification failed."

    def test_404_when_google_signin_not_configured(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _UNCONFIGURED_SETTINGS)
        response = client.post("/auth/google", json={"credential": "anything"})
        assert response.status_code == 404

    def test_empty_credential_is_rejected_by_schema_validation(self, client: TestClient):
        response = client.post("/auth/google", json={"credential": ""})
        assert response.status_code == 422

    def test_missing_credential_field_is_rejected_by_schema_validation(self, client: TestClient):
        response = client.post("/auth/google", json={})
        assert response.status_code == 422

    def test_response_never_reflects_the_raw_credential_back(self, client: TestClient):
        secret_looking_credential = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.fakefakefake"
        with patch.object(
            identity_auth_mod,
            "verify_google_id_token",
            side_effect=GoogleTokenValidationError("Google sign-in verification failed."),
        ):
            response = client.post("/auth/google", json={"credential": secret_looking_credential})
        assert secret_looking_credential not in response.text


class TestGoogleLinkAndUnlink:
    def _register_and_login(self, client: TestClient, email: str) -> str:
        response = client.post(
            "/auth/register",
            json={"email": email, "password": "correcthorse1", "display_name": "Test User"},
        )
        assert response.status_code == 201
        return response.json()["access_token"]

    def test_list_links_requires_authentication(self, client: TestClient):
        response = client.get("/auth/google/link")
        assert response.status_code == 401

    def test_link_requires_authentication(self, client: TestClient):
        response = client.post("/auth/google/link", json={"credential": "x"})
        assert response.status_code == 401

    def test_unlink_requires_authentication(self, client: TestClient):
        response = client.delete("/auth/google/link")
        assert response.status_code == 401

    def test_authenticated_user_can_link_a_google_identity(self, client: TestClient):
        token = self._register_and_login(client, "linkme@example.com")
        headers = {"Authorization": f"Bearer {token}"}

        list_before = client.get("/auth/google/link", headers=headers)
        assert list_before.json()["identities"] == []
        assert list_before.json()["has_password"] is True

        with patch.object(identity_auth_mod, "verify_google_id_token", return_value=_fake_claims()):
            link_response = client.post(
                "/auth/google/link", headers=headers, json={"credential": "fake-credential"}
            )
        assert link_response.status_code == 200
        assert len(link_response.json()["identities"]) == 1
        assert link_response.json()["identities"][0]["provider"] == "google"

    def test_linking_an_identity_already_owned_by_another_user_is_refused(self, client: TestClient):
        token_a = self._register_and_login(client, "usera@example.com")
        token_b = self._register_and_login(client, "userb@example.com")

        with patch.object(
            identity_auth_mod, "verify_google_id_token", return_value=_fake_claims(sub="shared-sub")
        ):
            first = client.post(
                "/auth/google/link",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"credential": "cred-a"},
            )
            assert first.status_code == 200

            second = client.post(
                "/auth/google/link",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"credential": "cred-b"},
            )
        assert second.status_code == 409

    def test_unlink_removes_the_link_when_a_password_exists(self, client: TestClient):
        token = self._register_and_login(client, "unlinkme@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        with patch.object(identity_auth_mod, "verify_google_id_token", return_value=_fake_claims()):
            client.post("/auth/google/link", headers=headers, json={"credential": "fake"})

        response = client.delete("/auth/google/link", headers=headers)
        assert response.status_code == 200
        assert response.json()["identities"] == []

    def test_unlink_refused_for_a_google_only_accounts_sole_identity(self, client: TestClient):
        with patch.object(identity_auth_mod, "verify_google_id_token", return_value=_fake_claims()):
            signin = client.post("/auth/google", json={"credential": "fake"})
        token = signin.json()["access_token"]

        response = client.delete("/auth/google/link", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 409


class TestGoogleOnlyAccountLocalLoginRegression:
    """Regression coverage for a real bug found while building this: a
    Google-only account (password_hash IS NULL) attempting the *local*
    email/password login used to crash with an unhandled AttributeError
    (argon2-cffi's verify() called with a None hash) instead of the same
    generic 401 every other login failure gets -- a real account-
    enumeration side channel via the HTTP status code alone."""

    def test_local_login_for_a_google_only_account_returns_ordinary_401_not_500(
        self, client: TestClient
    ):
        with patch.object(
            identity_auth_mod,
            "verify_google_id_token",
            return_value=_fake_claims(email="googleonly@example.com"),
        ):
            client.post("/auth/google", json={"credential": "fake"})

        response = client.post(
            "/auth/login", json={"email": "googleonly@example.com", "password": "anything123"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect email or password."

    def test_google_only_account_can_set_a_first_password_via_change_password(
        self, client: TestClient
    ):
        """change-password doubles as "set a local password" for a
        Google-only account -- no current password to verify, gated only
        by the existing authenticated-session requirement."""
        with patch.object(
            identity_auth_mod,
            "verify_google_id_token",
            return_value=_fake_claims(email="setpassword@example.com"),
        ):
            signin = client.post("/auth/google", json={"credential": "fake"})
        token = signin.json()["access_token"]

        response = client.post(
            "/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": "irrelevant", "new_password": "correcthorse1"},
        )
        assert response.status_code == 200

        # The new password now genuinely works for local login.
        login = client.post(
            "/auth/login",
            json={"email": "setpassword@example.com", "password": "correcthorse1"},
        )
        assert login.status_code == 200
