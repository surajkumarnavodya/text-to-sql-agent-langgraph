"""Unit tests for `security/oidc.py`'s JWT validation.

Fully offline -- a real RSA keypair is generated once per test session,
tokens are signed locally with `pyjwt`, and `jwt.PyJWKClient.fetch_data`
(the one method that would otherwise make a real HTTP call to an identity
provider) is monkeypatched to return a JWKS built from that same keypair.
No real network call, real identity provider, or real `.env` OIDC config
is ever involved.
"""

from __future__ import annotations

import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from config.settings import Settings
from security.oidc import (
    AuthIdentity,
    TokenValidationError,
    _cached_jwks_url,
    _get_jwk_client,
    extract_roles,
    validate_token,
)
from security.secrets import SecretStr

_ISSUER = "https://idp.example.com/"
_AUDIENCE = "my-api"
_JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
_KID = "test-key-1"


@pytest.fixture(scope="module")
def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def _other_keypair():
    """A second, unrelated keypair -- used to simulate a token signed by
    someone who does NOT hold the real provider's private key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def _reset_caches():
    """`_get_jwk_client`/`_cached_jwks_url` are process-lifetime `lru_cache`s
    by design (see their own docstrings) -- cleared before every test so
    one test's monkeypatched JWKS response can't leak into another's."""
    _get_jwk_client.cache_clear()
    _cached_jwks_url.cache_clear()
    yield
    _get_jwk_client.cache_clear()
    _cached_jwks_url.cache_clear()


@pytest.fixture(autouse=True)
def _mock_jwks_fetch(monkeypatch, _keypair):
    """Stands in for the real HTTP call `PyJWKClient.fetch_data` would make
    -- returns a JWKS document built from `_keypair`'s public key."""
    _private_key, public_key = _keypair
    jwk_dict = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    jwk_dict["kid"] = _KID
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    monkeypatch.setattr("jwt.PyJWKClient.fetch_data", lambda self: {"keys": [jwk_dict]})


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
        chroma_persist_dir="/tmp/chroma",
        chroma_collection_name="x",
        embedding_model_name="x",
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


def _make_token(
    private_key,
    *,
    claims: dict | None = None,
    kid: str | None = _KID,
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    expires_in: float = 3600,
    issued_delta: float = 0,
) -> str:
    now = time.time()
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-123",
        "iat": now - issued_delta,
        "exp": now + expires_in,
    }
    if claims:
        payload.update(claims)
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, private_key, algorithm="RS256", headers=headers)


class TestValidateTokenSuccess:
    def test_valid_token_returns_identity(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key, claims={"roles": ["admin", "analyst"]})
        identity = validate_token(token, _settings())

        assert identity == AuthIdentity(subject="user-123", roles=("admin", "analyst"), mode="oidc")

    def test_valid_token_with_no_roles_claim_gets_empty_roles(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key)
        identity = validate_token(token, _settings())
        assert identity.roles == ()

    def test_custom_role_claim_name_is_honored(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key, claims={"custom_roles": "viewer"})
        identity = validate_token(token, _settings(oidc_role_claim="custom_roles"))
        assert identity.roles == ("viewer",)


class TestValidateTokenRejections:
    def test_expired_token_is_rejected(self, _keypair, caplog):
        private_key, _ = _keypair
        token = _make_token(private_key, expires_in=-300)
        with (
            caplog.at_level(logging.WARNING, logger="security.audit"),
            pytest.raises(TokenValidationError, match="expired"),
        ):
            validate_token(token, _settings())
        assert any("reason='expired'" in r.message for r in caplog.records)

    def test_wrong_issuer_is_rejected(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key, issuer="https://evil.example.com/")
        with pytest.raises(TokenValidationError, match="issuer"):
            validate_token(token, _settings())

    def test_wrong_audience_is_rejected(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key, audience="someone-elses-api")
        with pytest.raises(TokenValidationError, match="audience"):
            validate_token(token, _settings())

    def test_signature_from_an_unrelated_key_is_rejected(self, _other_keypair):
        """The critical case: a token that is otherwise perfectly
        well-formed (right issuer, right audience, not expired) but was
        never actually signed by the real provider's private key -- this
        is what an attacker who can forge claims but not the signature
        would produce."""
        forger_private_key, _ = _other_keypair
        token = _make_token(forger_private_key)
        with pytest.raises(TokenValidationError, match="signature|invalid"):
            validate_token(token, _settings())

    def test_missing_subject_claim_is_rejected(self, _keypair):
        private_key, _ = _keypair
        now = time.time()
        payload = {"iss": _ISSUER, "aud": _AUDIENCE, "iat": now, "exp": now + 3600}
        token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": _KID})
        with pytest.raises(TokenValidationError, match="claim"):
            validate_token(token, _settings())

    def test_unknown_kid_is_rejected(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key, kid="some-other-key-id")
        with pytest.raises(TokenValidationError):
            validate_token(token, _settings())

    def test_alg_none_attack_is_rejected(self, _keypair):
        """A token that claims `alg: none` (no signature at all) must never
        be accepted, regardless of what its claims say -- the classic JWT
        'alg confusion' bypass. `validate_token` never reads `alg` from the
        token; the explicit `algorithms=["RS256"]` allowlist alone must
        reject this."""
        now = time.time()
        payload = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": "user-123",
            "iat": now,
            "exp": now + 3600,
        }
        # jwt.encode deliberately no longer supports alg="none" as an easy
        # top-level call -- construct the header.payload. form by hand to
        # simulate what an attacker would actually send.
        import base64
        import json

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        body = _b64(json.dumps(payload).encode())
        forged_token = f"{header}.{body}."

        with pytest.raises(TokenValidationError):
            validate_token(forged_token, _settings())

    def test_oidc_not_configured_is_rejected(self, _keypair):
        private_key, _ = _keypair
        token = _make_token(private_key)
        with pytest.raises(TokenValidationError, match="not configured"):
            validate_token(token, _settings(oidc_issuer=None, oidc_audience=None))

    def test_malformed_token_is_rejected(self):
        with pytest.raises(TokenValidationError):
            validate_token("not-a-jwt-at-all", _settings())

    def test_rejection_never_logs_the_raw_token(self, _keypair, caplog):
        private_key, _ = _keypair
        token = _make_token(private_key, expires_in=-300)
        with (
            caplog.at_level(logging.WARNING, logger="security.audit"),
            pytest.raises(TokenValidationError),
        ):
            validate_token(token, _settings())
        assert not any(token in r.message for r in caplog.records)


class TestExtractRoles:
    def test_single_string_role(self):
        assert extract_roles({"roles": "admin"}, "roles") == ("admin",)

    def test_list_of_roles(self):
        assert extract_roles({"roles": ["admin", "viewer"]}, "roles") == ("admin", "viewer")

    def test_missing_claim_returns_empty(self):
        assert extract_roles({}, "roles") == ()

    def test_empty_string_returns_empty(self):
        assert extract_roles({"roles": ""}, "roles") == ()

    def test_non_string_list_items_are_filtered_out(self):
        assert extract_roles({"roles": ["admin", 123, None, "viewer"]}, "roles") == (
            "admin",
            "viewer",
        )

    def test_unexpected_type_returns_empty(self):
        assert extract_roles({"roles": {"admin": True}}, "roles") == ()
