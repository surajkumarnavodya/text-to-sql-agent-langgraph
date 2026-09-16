"""Unit tests for `api/auth.py`'s `verify_api_key` dependency.

The 401-vs-200 behavior itself is already covered end-to-end by
`tests/test_api_ask.py::TestAsk::test_auth_required_when_token_configured`
and `TestSchemaTables::test_requires_auth_when_token_configured`. This file
is specifically the regression evidence for the 2026 Phase 1 security
review's MON-02 finding: an authentication failure previously left no
audit-log record at all. Kept separate rather than folded into
`tests/test_api_ask.py`, matching this project's "one file per audit
finding/control" convention (see `tests/test_nodes_security_wiring.py`'s
own docstring).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
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
    api_auth_token=SecretStr("s3cret"),
)


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
    monkeypatch.setattr("api.auth.get_settings", lambda: _BASE_SETTINGS)
    return _BASE_SETTINGS


@pytest.fixture(autouse=True)
def _reset_ip_limiters():
    api_main._ip_limiters.clear()
    yield
    api_main._ip_limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestAuthFailureAuditLogging:
    def test_missing_header_logs_auth_failed(self, client, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            response = client.get("/schema/tables")

        assert response.status_code == 401
        events = [r.message for r in caplog.records if "event=auth_failed" in r.message]
        assert len(events) == 1
        assert "reason='missing_header'" in events[0]

    def test_wrong_token_logs_auth_failed(self, client, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            response = client.get("/schema/tables", headers={"Authorization": "Bearer wrong"})

        assert response.status_code == 401
        events = [r.message for r in caplog.records if "event=auth_failed" in r.message]
        assert len(events) == 1
        assert "reason='invalid_token'" in events[0]

    def test_attempted_token_value_is_never_logged(self, client, caplog):
        """The audit event must be useful for detecting brute-force/guessing
        attempts without itself becoming a place a guessed-but-wrong
        candidate token leaks to (logs are a lower-trust sink than the
        request path itself -- see `security/redaction.py`'s module
        docstring for the same principle applied elsewhere)."""
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            client.get(
                "/schema/tables",
                headers={"Authorization": "Bearer guessed-secret-value"},
            )

        assert not any("guessed-secret-value" in r.message for r in caplog.records)

    def test_correct_token_logs_nothing(self, client, caplog, monkeypatch):
        monkeypatch.setattr("api.main.get_read_only_engine", lambda settings: object())
        monkeypatch.setattr("api.main.introspect_schema", lambda engine, schema: [])

        with caplog.at_level(logging.WARNING, logger="security.audit"):
            response = client.get("/schema/tables", headers={"Authorization": "Bearer s3cret"})

        assert response.status_code == 200
        assert not any("event=auth_failed" in r.message for r in caplog.records)
