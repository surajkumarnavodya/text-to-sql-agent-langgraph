"""Unit tests for security/google_oidc.py -- the server-side Google ID
token trust boundary.

`google.oauth2.id_token.verify_oauth2_token` (the actual signature/issuer/
expiry verification against Google's real rotating public keys) is mocked
throughout -- there is no way to produce a token this library will accept
without a real Google account completing a real sign-in (see
`docs/AUTHENTICATION.md`'s own disclosure on what is and isn't
live-verified). What *is* tested here, fully for real: every check this
module layers *on top of* that library call (`aud`/`azp` already covered
by the mocked library call itself; `sub`/`email`/`email_verified`/`hd`/
`nonce` presence and validity are this module's own code, not the
library's), the safe-error-message contract, and the nonce store's
single-use/expiry behavior (no mocking needed -- it's this module's own
pure logic).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from google.auth.exceptions import GoogleAuthError

from config.settings import Settings
from security.google_oidc import (
    GoogleTokenValidationError,
    consume_signin_nonce,
    issue_signin_nonce,
    verify_google_id_token,
)
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
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
    log_level="INFO",
    log_redaction_level="standard",
    local_auth_enabled=True,
    auth_database_url=SecretStr("postgresql+psycopg2://x:y@localhost/identity"),
    jwt_secret_key=SecretStr("x" * 32),
    google_oauth_client_id="test-client-id.apps.googleusercontent.com",
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


def _valid_claims(**overrides: object) -> dict:
    claims = {
        "sub": "110044332232098765432",
        "email": "user@example.com",
        "email_verified": True,
        "name": "Test User",
        "aud": "test-client-id.apps.googleusercontent.com",
        "iss": "https://accounts.google.com",
    }
    claims.update(overrides)
    return claims


class TestVerifyGoogleIdTokenBasicValidation:
    def test_not_configured_raises_before_any_verification(self):
        settings = _settings(google_oauth_client_id=None)
        with pytest.raises(GoogleTokenValidationError, match="not configured"):
            verify_google_id_token("anything", settings)

    def test_empty_credential_is_rejected(self):
        with pytest.raises(GoogleTokenValidationError):
            verify_google_id_token("", _settings())

    def test_oversized_credential_is_rejected_before_verification(self):
        with patch("security.google_oidc.verify_oauth2_token") as mock_verify:
            with pytest.raises(GoogleTokenValidationError):
                verify_google_id_token("x" * 9000, _settings())
            mock_verify.assert_not_called()


class TestVerifyGoogleIdTokenClaimValidation:
    """The library call itself is mocked to return a claims dict as if
    signature/iss/aud/exp had already passed -- these tests exercise this
    module's *own* additional checks on top of that."""

    def test_valid_claims_return_expected_identity(self):
        with patch("security.google_oidc.verify_oauth2_token", return_value=_valid_claims()):
            result = verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert result.sub == "110044332232098765432"
        assert result.email == "user@example.com"
        assert result.email_verified is True
        assert result.name == "Test User"
        assert result.hd is None

    def test_library_value_error_is_wrapped_safely(self):
        """The library raises a bare ValueError for signature failure,
        expiry, wrong audience, and malformed tokens alike -- the wrapped
        error must never leak that raw message (which can echo token-
        internal detail)."""
        with (
            patch(
                "security.google_oidc.verify_oauth2_token",
                side_effect=ValueError("Token used too early, 1234 < 5678"),
            ),
            pytest.raises(GoogleTokenValidationError) as exc_info,
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert "1234" not in str(exc_info.value)
        assert "5678" not in str(exc_info.value)

    def test_google_auth_error_issuer_rejection_is_wrapped_safely(self):
        with (
            patch(
                "security.google_oidc.verify_oauth2_token",
                side_effect=GoogleAuthError("Wrong issuer: evil.example.com"),
            ),
            pytest.raises(GoogleTokenValidationError) as exc_info,
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert "evil.example.com" not in str(exc_info.value)

    def test_missing_subject_is_rejected(self):
        claims = _valid_claims()
        del claims["sub"]
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=False)

    def test_missing_email_is_rejected(self):
        claims = _valid_claims()
        del claims["email"]
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=False)

    def test_email_verified_false_is_not_rejected_but_reported_accurately(self):
        """An unverified email is still a legitimate sub to sign in with --
        this function reports it accurately, it's the *caller*'s job to
        decide what to do with an unverified email (see
        identity.repositories.external_identities's own docstring)."""
        claims = _valid_claims(email_verified=False)
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert result.email_verified is False

    def test_missing_email_verified_claim_defaults_to_false_not_true(self):
        """Never assume every Google profile has all optional claims --
        an absent email_verified claim must fail closed (False), not be
        silently treated as verified."""
        claims = _valid_claims()
        del claims["email_verified"]
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert result.email_verified is False

    def test_azp_matching_client_id_is_accepted(self):
        claims = _valid_claims(azp="test-client-id.apps.googleusercontent.com")
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert result.sub == claims["sub"]

    def test_azp_mismatch_is_rejected(self):
        claims = _valid_claims(azp="some-other-client-id.apps.googleusercontent.com")
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=False)

    def test_hosted_domain_spoof_via_email_suffix_alone_is_rejected(self):
        """A workspace restriction must be checked against the *signed*
        `hd` claim, never inferred from the email's own @domain suffix --
        an attacker fully controls the email string's shape in a token
        forged for a different domain, but cannot forge Google's own
        signed hd claim."""
        settings = _settings(google_oauth_allowed_hosted_domains=("example.com",))
        claims = _valid_claims(email="user@example.com")  # no "hd" claim at all
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", settings, require_known_nonce=False)

    def test_hosted_domain_matching_allowlist_is_accepted(self):
        settings = _settings(google_oauth_allowed_hosted_domains=("example.com",))
        claims = _valid_claims(hd="example.com")
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", settings, require_known_nonce=False)
        assert result.hd == "example.com"

    def test_hosted_domain_not_in_allowlist_is_rejected(self):
        settings = _settings(google_oauth_allowed_hosted_domains=("example.com",))
        claims = _valid_claims(hd="not-allowed.com")
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", settings, require_known_nonce=False)

    def test_no_hosted_domain_restriction_configured_allows_any_account(self):
        claims = _valid_claims()  # ordinary consumer account, no hd claim
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert result.hd is None


class TestNonceRequirement:
    def test_valid_known_nonce_is_accepted_and_consumed(self):
        settings = _settings()
        nonce = issue_signin_nonce(settings)
        claims = _valid_claims(nonce=nonce)
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", settings, require_known_nonce=True)
        assert result.sub == claims["sub"]

    def test_replaying_the_same_nonce_a_second_time_is_rejected(self):
        settings = _settings()
        nonce = issue_signin_nonce(settings)
        claims = _valid_claims(nonce=nonce)
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            verify_google_id_token("fake-token", settings, require_known_nonce=True)
            with pytest.raises(GoogleTokenValidationError):
                verify_google_id_token("fake-token", settings, require_known_nonce=True)

    def test_unknown_nonce_is_rejected(self):
        claims = _valid_claims(nonce="a-nonce-this-server-never-issued")
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=True)

    def test_missing_nonce_claim_is_rejected_when_required(self):
        claims = _valid_claims()  # no "nonce" key at all
        with (
            patch("security.google_oidc.verify_oauth2_token", return_value=claims),
            pytest.raises(GoogleTokenValidationError),
        ):
            verify_google_id_token("fake-token", _settings(), require_known_nonce=True)

    def test_nonce_check_skipped_entirely_when_not_required(self):
        claims = _valid_claims()  # no nonce, but require_known_nonce=False
        with patch("security.google_oidc.verify_oauth2_token", return_value=claims):
            result = verify_google_id_token("fake-token", _settings(), require_known_nonce=False)
        assert result.sub == claims["sub"]


class TestNonceStore:
    def test_issued_nonce_is_a_high_entropy_string(self):
        settings = _settings()
        nonce = issue_signin_nonce(settings)
        assert isinstance(nonce, str)
        assert len(nonce) >= 32

    def test_two_issued_nonces_are_distinct(self):
        settings = _settings()
        assert issue_signin_nonce(settings) != issue_signin_nonce(settings)

    def test_consume_is_single_use(self):
        settings = _settings()
        nonce = issue_signin_nonce(settings)
        assert consume_signin_nonce(nonce) is True
        assert consume_signin_nonce(nonce) is False

    def test_consuming_an_unissued_nonce_returns_false(self):
        assert consume_signin_nonce("never-issued-by-anyone") is False

    def test_expired_nonce_is_rejected(self):
        settings = _settings(google_oauth_nonce_ttl_seconds=1)
        nonce = issue_signin_nonce(settings)
        with patch("security.google_oidc.time.monotonic", return_value=1e12):
            assert consume_signin_nonce(nonce) is False
