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
