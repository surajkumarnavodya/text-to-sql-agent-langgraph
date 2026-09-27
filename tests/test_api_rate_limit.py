"""Unit tests for api/rate_limit.py -- the shared per-client-IP limiter
used by /execute, /schema/refresh, the mutating /documents routes, and
/generate/confirm (see that module's own docstring for why it exists:
these routes previously had no rate limit at all, unlike /ask)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.rate_limit import _limiters, enforce_api_action_rate_limit
from config.settings import Settings
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
)


@pytest.fixture(autouse=True)
def _reset_limiters():
    _limiters.clear()
    yield
    _limiters.clear()


def _fake_request(client_host: str | None = "1.2.3.4") -> Request:
    """A minimal ASGI scope good enough for `Request.client.host`."""
    scope = {
        "type": "http",
        "client": (client_host, 12345) if client_host else None,
        "headers": [],
    }
    return Request(scope)


class TestEnforceApiActionRateLimit:
    def test_allows_calls_under_the_limit(self):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 2})
        request = _fake_request()
        enforce_api_action_rate_limit(request, "test_action", settings)
        enforce_api_action_rate_limit(request, "test_action", settings)  # must not raise

    def test_raises_429_once_the_limit_is_exceeded(self):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        request = _fake_request()
        enforce_api_action_rate_limit(request, "test_action", settings)
        with pytest.raises(HTTPException) as exc_info:
            enforce_api_action_rate_limit(request, "test_action", settings)
        assert exc_info.value.status_code == 429
        assert "Retry-After" in exc_info.value.headers

    def test_different_actions_have_independent_budgets(self):
        """Hammering /execute must not consume /schema/refresh's budget --
        each action gets its own limiter, even for the same client IP."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        request = _fake_request()
        enforce_api_action_rate_limit(request, "execute", settings)
        enforce_api_action_rate_limit(request, "schema_refresh", settings)  # must not raise

    def test_different_client_ips_have_independent_budgets(self):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        enforce_api_action_rate_limit(_fake_request("1.1.1.1"), "execute", settings)
        enforce_api_action_rate_limit(
            _fake_request("2.2.2.2"), "execute", settings
        )  # must not raise

    def test_missing_client_falls_back_to_a_shared_unknown_bucket(self):
        """A request with no `request.client` (e.g. certain test/proxy
        setups) must still be governed by a limit, not silently exempted."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        enforce_api_action_rate_limit(_fake_request(None), "execute", settings)
        with pytest.raises(HTTPException):
            enforce_api_action_rate_limit(_fake_request(None), "execute", settings)


class TestEnforceApiActionRateLimitIdentityAware:
    """Enterprise scalability assessment (2026-09-27): `identity` is a new
    optional parameter -- every route that already resolves an
    `AuthIdentity` (`/execute`, `/schema/refresh`, `/generate/confirm`,
    `/search/media`, `POST`/`DELETE /documents`, every `/attachments/*`
    action route) now passes it through, closing a gap where this limiter
    rate-limited purely by IP even for an authenticated caller."""

    def test_two_authenticated_users_behind_the_same_ip_get_independent_budgets(self):
        """The actual bug this fixes: two real, distinct authenticated
        users sharing one IP (a NAT'd office, a corporate VPN) must not
        share one rate-limit bucket just because IP was all that was ever
        consulted."""
        from security.oidc import AuthIdentity

        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        request = _fake_request("10.0.0.5")  # same IP for both callers
        alice = AuthIdentity(subject="alice", roles=("user",), mode="local")
        bob = AuthIdentity(subject="bob", roles=("user",), mode="local")

        enforce_api_action_rate_limit(request, "execute", settings, identity=alice)
        enforce_api_action_rate_limit(request, "execute", settings, identity=bob)  # must not raise

    def test_same_authenticated_user_is_still_limited_across_different_ips(self):
        """The inverse property: identity, not IP, is authoritative once an
        identity exists -- one real user can't dodge their own limit by
        changing IP (a mobile network handoff, a VPN)."""
        from security.oidc import AuthIdentity

        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        alice = AuthIdentity(subject="alice", roles=("user",), mode="local")

        enforce_api_action_rate_limit(
            _fake_request("10.0.0.5"), "execute", settings, identity=alice
        )
        with pytest.raises(HTTPException):
            enforce_api_action_rate_limit(
                _fake_request("10.0.0.9"), "execute", settings, identity=alice
            )

    def test_no_real_identity_still_falls_back_to_ip(self):
        """'none'/'static_token' auth modes have no real per-caller
        identity (see `security.oidc.real_caller_subject`) -- must still
        fall back to IP-based keying exactly as before this parameter
        existed, not silently pool every such caller together."""
        from security.oidc import AuthIdentity

        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        shared_identity = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")

        enforce_api_action_rate_limit(
            _fake_request("1.1.1.1"), "execute", settings, identity=shared_identity
        )
        enforce_api_action_rate_limit(
            _fake_request("2.2.2.2"), "execute", settings, identity=shared_identity
        )  # must not raise -- different IPs, same fallback-only "identity"

    def test_identity_none_is_the_original_ip_only_behavior(self):
        """The default (`identity=None`, every pre-existing call site
        before this parameter existed) is unaffected."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        enforce_api_action_rate_limit(_fake_request("1.1.1.1"), "execute", settings)
        with pytest.raises(HTTPException):
            enforce_api_action_rate_limit(_fake_request("1.1.1.1"), "execute", settings)
