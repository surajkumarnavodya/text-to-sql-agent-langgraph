"""Unit tests for identity/security.py -- password hashing, locally-issued
JWT access tokens, and opaque refresh tokens."""

from __future__ import annotations

from datetime import UTC

import pytest
from identity.exceptions import LocalTokenValidationError
from identity.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    looks_like_local_token,
    validate_local_token,
    verify_password,
)

from config.settings import Settings


def _hs256_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        local_auth_enabled=True,
        auth_database_url="postgresql://placeholder",
        jwt_secret_key="s" * 40,
        jwt_issuer="text-to-sql-agent",
        jwt_audience="text-to-sql-web",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestPasswordHashing:
    def test_hash_is_not_the_plaintext(self):
        assert hash_password("hunter2hunter2") != "hunter2hunter2"

    def test_verify_accepts_correct_password(self):
        h = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", h) is True

    def test_verify_rejects_wrong_password(self):
        h = hash_password("correct horse battery staple")
        assert verify_password("wrong password", h) is False

    def test_verify_never_raises_on_malformed_hash(self):
        assert verify_password("anything", "not-a-real-argon2-hash") is False


class TestAccessTokens:
    def test_round_trips_subject_and_roles(self):
        settings = _hs256_settings()
        token = create_access_token("user-123", ("user", "analyst"), settings)
        claims = validate_local_token(token, settings)
        assert claims.subject == "user-123"
        assert claims.roles == ("user", "analyst")

    def test_looks_like_local_token_true_for_own_issuer(self):
        settings = _hs256_settings()
        token = create_access_token("user-123", ("user",), settings)
        assert looks_like_local_token(token, settings) is True

    def test_looks_like_local_token_false_for_different_issuer(self):
        settings = _hs256_settings()
        token = create_access_token("user-123", ("user",), settings)
        other_settings = _hs256_settings(jwt_issuer="a-different-issuer")
        assert looks_like_local_token(token, other_settings) is False

    def test_looks_like_local_token_false_for_garbage(self):
        settings = _hs256_settings()
        assert looks_like_local_token("not.a.jwt", settings) is False

    def test_validate_rejects_wrong_audience(self):
        settings = _hs256_settings()
        token = create_access_token("user-123", ("user",), settings)
        wrong_audience_settings = _hs256_settings(jwt_audience="some-other-app")
        with pytest.raises(LocalTokenValidationError):
            validate_local_token(token, wrong_audience_settings)

    def test_validate_rejects_wrong_signing_secret(self):
        settings = _hs256_settings()
        token = create_access_token("user-123", ("user",), settings)
        wrong_secret_settings = _hs256_settings(jwt_secret_key="t" * 40)
        with pytest.raises(LocalTokenValidationError):
            validate_local_token(token, wrong_secret_settings)

    def test_validate_rejects_expired_token(self):
        settings = _hs256_settings(access_token_expire_minutes=1)
        # Crafts a token with `exp` already in the past directly, rather
        # than sleeping in a unit test.
        from datetime import datetime, timedelta

        import jwt as pyjwt

        past_claims = {
            "sub": "user-123",
            "roles": ["user"],
            "token_type": "access",
            "iat": datetime.now(UTC) - timedelta(hours=1),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
            "jti": "test",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        }
        expired_token = pyjwt.encode(
            past_claims, settings.jwt_secret_key.get_secret_value(), algorithm="HS256"
        )
        with pytest.raises(LocalTokenValidationError):
            validate_local_token(expired_token, settings)


class TestRefreshTokens:
    def test_generated_tokens_are_high_entropy_and_unique(self):
        a = generate_refresh_token()
        b = generate_refresh_token()
        assert a != b
        assert len(a) > 32

    def test_hash_is_deterministic(self):
        token = generate_refresh_token()
        assert hash_refresh_token(token) == hash_refresh_token(token)

    def test_hash_differs_for_different_tokens(self):
        assert hash_refresh_token(generate_refresh_token()) != hash_refresh_token(
            generate_refresh_token()
        )

    def test_hash_is_not_the_raw_token(self):
        token = generate_refresh_token()
        assert hash_refresh_token(token) != token
