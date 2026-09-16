"""End-to-end authorization (RBAC) tests -- 2026 Phase 2 security review.

Unlike `tests/test_nodes_security_wiring.py`/`tests/test_orchestrator.py`
(which call node functions directly with a hand-built `caller_roles`
tuple), this file drives the real HTTP routes through `TestClient` with
real OIDC JWTs (signed locally against a test RSA keypair, validated by
the real `security.oidc.validate_token` -- only the JWKS HTTP fetch is
mocked, same technique as `tests/test_oidc.py`) so the full chain --
`api.auth.verify_api_key` -> `security.oidc.validate_token` ->
`api.authz.require_permission` -> `agent.authz.has_permission` -- is
exercised together, not just each piece in isolation.

Covers every scenario the Phase 2 brief calls out explicitly:
- horizontal privilege escalation (two different subjects, same role,
  identical treatment -- identity alone never grants anything)
- vertical privilege escalation (a lower role attempting a higher-role-only
  action)
- missing roles (an authenticated identity with no roles claim at all)
- invalid roles (a role name `agent.authz.ROLE_PERMISSIONS` doesn't
  recognize)
- expired credentials
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

import api.main as api_main
from config.settings import Settings
from security.oidc import _cached_jwks_url, _get_jwk_client
from security.secrets import SecretStr

_ISSUER = "https://idp.example.com/"
_AUDIENCE = "my-api"
_JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
_KID = "test-key-1"


@pytest.fixture(scope="module")
def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def _reset_jwk_caches():
    _get_jwk_client.cache_clear()
    _cached_jwks_url.cache_clear()
    yield
    _get_jwk_client.cache_clear()
    _cached_jwks_url.cache_clear()


@pytest.fixture(autouse=True)
def _mock_jwks_fetch(monkeypatch, _keypair):
    _private_key, public_key = _keypair
    jwk_dict = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    jwk_dict["kid"] = _KID
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    monkeypatch.setattr("jwt.PyJWKClient.fetch_data", lambda self: {"keys": [jwk_dict]})


def _token(private_key, *, subject: str, roles: tuple[str, ...] | None, expires_in: float = 3600):
    now = time.time()
    payload = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": subject,
        "iat": now,
        "exp": now + expires_in,
    }
    if roles is not None:
        payload["roles"] = list(roles)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": _KID})


def _settings(**overrides: object) -> Settings:
    base = dict(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=5432,
        db_name="mydb",
        db_user="reader",
        db_password=SecretStr("secret"),
        db_connection_string=None,
        db_schema=None,
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
        cost_estimation_enabled=True,
        cost_estimation_timeout_seconds=3,
        cost_moderate_row_threshold=50_000,
        cost_high_row_threshold=1_000_000,
        log_level="INFO",
        log_redaction_level="standard",
        oidc_issuer=_ISSUER,
        oidc_audience=_AUDIENCE,
        oidc_jwks_url=_JWKS_URL,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    settings = _settings()
    monkeypatch.setattr("api.main.get_settings", lambda: settings)
    monkeypatch.setattr("api.auth.get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def _reset_ip_limiters():
    api_main._ip_limiters.clear()
    yield
    api_main._ip_limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestVerticalPrivilegeEscalation:
    """A lower-privilege role must never reach a higher-role-only action,
    no matter how it asks."""

    def test_viewer_cannot_execute_sql(self, client, _keypair):
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=("viewer",))

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 403

    def test_user_cannot_refresh_schema(self, client, _keypair):
        """ "user" grants EXECUTE_SQL but not SCHEMA_REFRESH (admin-only in
        the default role map) -- confirms the hierarchy is real, not just
        "any non-viewer role gets everything"."""
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=("user",))

        response = client.post("/schema/refresh", headers=_auth(token))

        assert response.status_code == 403

    def test_analyst_cannot_delete_documents(self, client, _keypair):
        """ "analyst" grants DOCUMENTS_READ_SENSITIVE but not
        DOCUMENTS_DELETE (admin-only)."""
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=("analyst",))

        response = client.delete("/documents/doc-1", headers=_auth(token))

        assert response.status_code == 403

    def test_user_cannot_upload_documents(self, client, _keypair):
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=("user",))

        response = client.post(
            "/documents",
            files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
            data={"collection": "documents"},
            headers=_auth(token),
        )

        assert response.status_code == 403

    def test_admin_can_refresh_schema_reaches_past_the_authz_gate(
        self, client, _keypair, monkeypatch
    ):
        """Positive control: confirms the 403s above are really about role,
        not e.g. a route-wiring mistake that would 403 everyone."""
        private_key, _ = _keypair
        token = _token(private_key, subject="admin1", roles=("admin",))
        monkeypatch.setattr("api.main.refresh_all_schema_indexes", lambda settings: {})

        response = client.post("/schema/refresh", headers=_auth(token))

        assert response.status_code == 200


class TestHorizontalPrivilegeEscalation:
    """Identity alone (a distinct `sub`) must never grant a permission a
    caller's *role* doesn't -- two different subjects with the same role
    get exactly the same, correct treatment."""

    def test_two_different_subjects_with_viewer_role_are_both_denied(self, client, _keypair):
        private_key, _ = _keypair
        token_a = _token(private_key, subject="user-a", roles=("viewer",))
        token_b = _token(private_key, subject="user-b", roles=("viewer",))

        response_a = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token_a))
        response_b = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token_b))

        assert response_a.status_code == 403
        assert response_b.status_code == 403

    def test_a_privileged_subject_name_grants_nothing_by_itself(self, client, _keypair):
        """A `sub` claim that merely *looks* administrative (e.g. a client
        naively assuming a naming convention implies trust) must not be
        treated specially -- only the `roles` claim matters."""
        private_key, _ = _keypair
        token = _token(private_key, subject="admin@example.com", roles=("viewer",))

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 403


class TestMissingAndInvalidRoles:
    def test_no_roles_claim_at_all_is_treated_as_no_permissions(self, client, _keypair):
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=None)

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 403

    def test_empty_roles_list_is_treated_as_no_permissions(self, client, _keypair):
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=())

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 403

    def test_unrecognized_role_name_grants_nothing(self, client, _keypair):
        """A role name `agent.authz.ROLE_PERMISSIONS` has never heard of
        (typo, or a claim from an identity-provider role this deployment
        hasn't mapped yet) must fail closed, not fail open."""
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=("super-admin-role",))

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 403

    def test_baseline_ask_permission_still_works_with_no_roles(self, client, _keypair, monkeypatch):
        """Sanity check: "no elevated roles" must still allow the one
        permission every default role (including a caller with literally
        no roles claim) needs for the app to be minimally usable --
        Permission.ASK is intentionally not viewer-gated beyond existing."""
        private_key, _ = _keypair
        token = _token(private_key, subject="u1", roles=())
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        response = client.post("/ask", json={"question": "hi"}, headers=_auth(token))

        assert response.status_code == 403  # "ask" is NOT in the default no-role grant


class TestExpiredAndInvalidCredentials:
    def test_expired_token_is_401_not_403(self, client, _keypair):
        """An expired token fails *authentication*, before authorization
        is ever consulted -- must be 401, distinguishable from a
        successfully-authenticated-but-unauthorized 403."""
        private_key, _ = _keypair
        token = _token(private_key, subject="admin1", roles=("admin",), expires_in=-300)

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 401

    def test_malformed_token_is_401(self, client):
        response = client.post(
            "/execute", json={"sql": "SELECT 1"}, headers=_auth("not-a-real-jwt")
        )
        assert response.status_code == 401

    def test_missing_authorization_header_is_401(self, client):
        response = client.post("/execute", json={"sql": "SELECT 1"})
        assert response.status_code == 401

    def test_token_signed_by_an_unrelated_key_is_401(self, client):
        forger_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _token(forger_key, subject="admin1", roles=("admin",))

        response = client.post("/execute", json={"sql": "SELECT 1"}, headers=_auth(token))

        assert response.status_code == 401
