"""Regression test: `api/rate_limit.py::enforce_api_action_rate_limit` keys
its per-client bucket on `request.client.host` (the actual TCP peer
address Starlette/uvicorn observed) and nothing else -- it never reads
`X-Forwarded-For`/`X-Real-IP`/any other client-suppliable header. That's
already the secure default per the master engagement brief's own rule
("Do not trust arbitrary client-provided IP headers unless they originate
from a trusted proxy") -- this codebase has no trusted-proxy configuration
at all, so trusting none of them is correct, not merely unfinished.

This file exists to *lock that in* as a named regression test, not to fix
a bug -- a future change adding `X-Forwarded-For` support for a real
reverse-proxy deployment (a legitimate, plausible future need) must not
accidentally make the header trusted by default with no proxy-allowlist
check, which is exactly the shape of bug this test would catch.

See `api/rate_limit.py`'s own docstring for the identical-shape precedent
this mirrors (`api/main.py`'s own `/ask` limiter), and
`tests/test_api_rate_limit.py` for the non-adversarial unit tests this
file deliberately does not duplicate (per-action/per-IP budget
independence, the 429 shape, the missing-client fallback).
"""

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
    api_action_rate_limit_per_minute=1,
)


@pytest.fixture(autouse=True)
def _reset_limiters():
    _limiters.clear()
    yield
    _limiters.clear()


def _fake_request(client_host: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "client": (client_host, 12345),
        "headers": headers or [],
    }
    return Request(scope)


def _header(name: str, value: str) -> tuple[bytes, bytes]:
    return (name.lower().encode(), value.encode())


class TestSpoofedForwardingHeadersCannotBypassOrPollutePerClientBudgets:
    def test_spoofed_x_forwarded_for_does_not_grant_a_fresh_budget(self):
        """Same real client IP, a *different*, attacker-chosen
        X-Forwarded-For on every call -- if the header were ever
        consulted, each call would look like a new client and dodge the
        limit entirely. It must not: all calls share one real-IP bucket."""
        real_ip = "203.0.113.7"
        request_1 = _fake_request(real_ip, [_header("X-Forwarded-For", "1.1.1.1")])
        request_2 = _fake_request(real_ip, [_header("X-Forwarded-For", "2.2.2.2")])

        enforce_api_action_rate_limit(request_1, "execute", _BASE_SETTINGS)
        with pytest.raises(HTTPException) as exc_info:
            enforce_api_action_rate_limit(request_2, "execute", _BASE_SETTINGS)
        assert exc_info.value.status_code == 429

    def test_spoofed_x_real_ip_does_not_grant_a_fresh_budget(self):
        real_ip = "203.0.113.7"
        request_1 = _fake_request(real_ip, [_header("X-Real-IP", "9.9.9.9")])
        request_2 = _fake_request(real_ip, [_header("X-Real-IP", "8.8.8.8")])

        enforce_api_action_rate_limit(request_1, "execute", _BASE_SETTINGS)
        with pytest.raises(HTTPException):
            enforce_api_action_rate_limit(request_2, "execute", _BASE_SETTINGS)

    def test_identical_spoofed_header_across_different_real_clients_does_not_pool_their_budgets(
        self,
    ):
        """Inverse check: two genuinely different clients that happen to
        send the *same* X-Forwarded-For value must not share a budget
        either -- proving the header isn't read at all, in either
        direction, rather than just "not read for the bypass case."""
        same_spoofed_header = [_header("X-Forwarded-For", "10.0.0.1")]
        request_a = _fake_request("198.51.100.1", same_spoofed_header)
        request_b = _fake_request("198.51.100.2", same_spoofed_header)

        enforce_api_action_rate_limit(request_a, "execute", _BASE_SETTINGS)
        enforce_api_action_rate_limit(request_b, "execute", _BASE_SETTINGS)  # must not raise

    def test_concurrent_identical_requests_from_the_same_real_ip_share_one_budget(self):
        """No header trickery at all -- confirms the baseline the tests
        above are actually testing against: the *real* client IP alone is
        what's rate-limited, and it works."""
        request_1 = _fake_request("203.0.113.7")
        request_2 = _fake_request("203.0.113.7")

        enforce_api_action_rate_limit(request_1, "execute", _BASE_SETTINGS)
        with pytest.raises(HTTPException):
            enforce_api_action_rate_limit(request_2, "execute", _BASE_SETTINGS)
